"""Sehat Saathi - elderly care coordination (Phase 1). Flask + SQLite."""
import os, re, sqlite3, smtplib, threading, time, secrets, json, urllib.request
import assistant
from datetime import datetime, timedelta
from email.message import EmailMessage
from flask import Flask, g, request, session, jsonify
from werkzeug.security import generate_password_hash as H, check_password_hash as C

DB = os.environ.get("DB_PATH", "care.db")
# minutes after due time: patient notified, reminder + marked unconfirmed, caregiver alerted, family alerted
ESC = [int(x) for x in os.environ.get("ESCALATION_MIN", "0,15,30,60").split(",")]
app = Flask(__name__, static_folder="static", static_url_path="")
def _secret():
    if os.environ.get("SECRET_KEY"): return os.environ["SECRET_KEY"]
    if not os.path.exists(".secret"): open(".secret", "w").write(secrets.token_hex(32))  # stable across restarts
    return open(".secret").read()
app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, pw TEXT);
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, name TEXT, age INT, notes TEXT, passcode TEXT);
CREATE TABLE IF NOT EXISTS access(user_id INT, patient_id INT, role TEXT, status TEXT DEFAULT 'active', PRIMARY KEY(user_id,patient_id));
CREATE TABLE IF NOT EXISTS checkins(patient_id INT, day TEXT, feeling TEXT, sleep TEXT, meals TEXT, help TEXT, by INT, PRIMARY KEY(patient_id,day));
CREATE TABLE IF NOT EXISTS medicines(id INTEGER PRIMARY KEY, patient_id INT, name TEXT, dosage TEXT, instructions TEXT,
  times TEXT, start TEXT, end TEXT, qty INT, active INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS doses(id INTEGER PRIMARY KEY, med_id INT, patient_id INT, due TEXT, status TEXT DEFAULT 'upcoming',
  by INT, note TEXT, at TEXT, UNIQUE(med_id,due));
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, patient_id INT, doctor TEXT, speciality TEXT, contact TEXT,
  when_at TEXT, place TEXT, notes TEXT, status TEXT DEFAULT 'scheduled');
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, patient_id INT, title TEXT, assignee INT, due TEXT,
  done INT DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, patient_id INT, user_id INT, kind TEXT, msg TEXT,
  key TEXT UNIQUE, ts TEXT, read INT DEFAULT 0, email TEXT, tries INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, patient_id INT, user_id INT, action TEXT, detail TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS i_doses ON doses(patient_id,due,status);
CREATE INDEX IF NOT EXISTS i_notif ON notifications(patient_id,user_id,id);
CREATE INDEX IF NOT EXISTS i_audit ON audit(patient_id,id);
CREATE INDEX IF NOT EXISTS i_appt ON appointments(patient_id,when_at);
"""
def now(): return datetime.now().replace(second=0, microsecond=0)
def iso(d): return d.strftime("%Y-%m-%dT%H:%M")
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db
@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d: d.close()
def init():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    try: c.execute("ALTER TABLE notifications ADD COLUMN tries INT DEFAULT 0")  # upgrade databases created before the email retry queue
    except sqlite3.OperationalError: pass
    c.commit(); c.close()
def rows(q, a=()): return [dict(r) for r in db().execute(q, a).fetchall()]
def one(q, a=()):
    r = db().execute(q, a).fetchone(); return dict(r) if r else None
def audit(pid, action, detail=""):
    db().execute("INSERT INTO audit(patient_id,user_id,action,detail,ts) VALUES(?,?,?,?,?)",
                 (pid, session.get("uid"), action, detail, iso(now())))

# ---------- notifications (real SMTP when configured, honest status otherwise) ----------
def send_email(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host: return "not_configured"
    try:
        m = EmailMessage(); m["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")); m["To"] = to
        m["Subject"] = subject; m.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=10) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"): s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(m)
        return "sent"
    except Exception as e:
        return "failed: " + str(e)[:80]
def notify(pid, uids, kind, msg, key):
    """Store an in-app alert and queue its email. Emails are sent by flush_emails() in the background,
    so a slow mail server never blocks a request."""
    d = db()
    for u in dict.fromkeys(uids):
        k = f"{key}-u{u}"  # duplicate prevention
        if d.execute("SELECT 1 FROM notifications WHERE key=?", (k,)).fetchone(): continue
        user = d.execute("SELECT email FROM users WHERE id=?", (u,)).fetchone()
        st = ("queued" if os.environ.get("SMTP_HOST") else "not_configured") if user else "no_email"
        d.execute("INSERT INTO notifications(patient_id,user_id,kind,msg,key,ts,email) VALUES(?,?,?,?,?,?,?)",
                  (pid, u, kind, msg, k, iso(now()), st))
    d.commit()
def flush_emails(limit=25):
    """Send queued emails; retry failed ones up to 3 attempts in total."""
    d = db()
    for n in rows("""SELECT n.id, n.kind, n.msg, u.email FROM notifications n JOIN users u ON u.id=n.user_id
                     WHERE n.email='queued' OR (n.email LIKE 'failed%' AND n.tries<3) ORDER BY n.id LIMIT ?""", (limit,)):
        st = send_email(n["email"], f"Sehat Saathi: {n['kind']}", n["msg"])
        d.execute("UPDATE notifications SET email=?, tries=tries+1 WHERE id=?", (st, n["id"])); d.commit()
def members(pid, role): return [r["user_id"] for r in rows("SELECT user_id FROM access WHERE patient_id=? AND role=? AND status='active'", (pid, role))]
def everyone_but_me(pid): return [r["user_id"] for r in rows("SELECT user_id FROM access WHERE patient_id=? AND status='active' AND user_id IS NOT ?", (pid, me()))]
def who(): return (one("SELECT name FROM users WHERE id=?", (me(),)) or {"name": "Someone"})["name"]

# ---------- dose generation + escalation ----------
def tick(t=None):
    t = t or now(); d = db(); today = t.strftime("%Y-%m-%d")
    for m in rows("SELECT * FROM medicines WHERE active=1"):
        if m["start"] and today < m["start"]: continue
        if m["end"] and today > m["end"]: continue
        for hm in (m["times"] or "").split(","):
            if hm.strip(): d.execute("INSERT OR IGNORE INTO doses(med_id,patient_id,due) VALUES(?,?,?)",
                                     (m["id"], m["patient_id"], f"{today}T{hm.strip()}"))
    for x in rows("""SELECT d.*, m.name mname, m.dosage, p.name pname FROM doses d JOIN medicines m ON m.id=d.med_id
                     JOIN patients p ON p.id=d.patient_id WHERE d.status IN ('upcoming','unconfirmed')
                     AND substr(d.due,1,10)>=?""", (today,)):
        late = (t - datetime.fromisoformat(x["due"])).total_seconds() / 60
        pid, tag, what = x["patient_id"], f"dose{x['id']}", f"{x['mname']} {x['dosage']} due {x['due'][11:]}"
        if late < ESC[0]:
            if late >= -10: notify(pid, members(pid, "patient"), "Upcoming dose", f"Coming up in a few minutes: {what}.", tag + "-pre")
            continue
        notify(pid, members(pid, "patient"), "Medicine due", f"Time to take {what}. Please confirm.", tag + "-s0")
        if late >= ESC[1]:
            if x["status"] == "upcoming": d.execute("UPDATE doses SET status='unconfirmed' WHERE id=?", (x["id"],))
            notify(pid, members(pid, "patient"), "Reminder", f"Not yet confirmed: {what}.", tag + "-s1")
        if late >= ESC[2]:
            notify(pid, members(pid, "caregiver"), "Possible missed dose",
                   f"{x['pname']} has not confirmed {what}. It may have been missed. Please check in and record what happened.", tag + "-s2")
        if late >= ESC[3]:
            notify(pid, members(pid, "family"), "Missed dose unresolved",
                   f"{x['pname']}'s dose is still unconfirmed and may have been missed: {what}. The caregiver was alerted earlier.", tag + "-s3")
    d.commit()
    for a in rows("SELECT * FROM appointments WHERE status='scheduled'"):  # appointment reminders: 24h and 2h ahead
        hrs = (datetime.fromisoformat(a["when_at"]) - t).total_seconds() / 3600
        pid = a["patient_id"]; ids = members(pid, "patient") + members(pid, "family") + members(pid, "caregiver")
        what = f"Dr {a['doctor']} on {a['when_at'].replace('T',' ')}" + (f" at {a['place']}" if a["place"] else "")
        if 0 <= hrs <= 24:  # the time is in the key, so a rescheduled appointment is announced again
            notify(pid, ids, "Appointment", f"Appointment with {what}." + (f" Notes: {a['notes']}" if a["notes"] else ""), f"appt{a['id']}-{a['when_at']}-24h")
        if 0 <= hrs <= 2:
            notify(pid, ids, "Appointment soon", f"Starting soon: appointment with {what}.", f"appt{a['id']}-{a['when_at']}-2h")
    for m in rows("SELECT * FROM medicines WHERE active=1 AND qty IS NOT NULL"):  # refill warning
        per_day = len([h for h in (m["times"] or "").split(",") if h.strip()]) or 1
        if m["qty"] / per_day <= 5:
            notify(m["patient_id"], members(m["patient_id"], "family"), "Refill needed",
                   f"{m['name']}: about {m['qty'] // per_day} days left.", f"refill{m['id']}-{m['qty']}")
def loop():
    while True:
        time.sleep(60)
        try:
            with app.app_context(): tick(); flush_emails()
        except Exception as e: print("tick error", e)

FAILS, LAST = {}, 0
def limited(k):
    n, t = FAILS.get(k, (0, 0)); return n >= 5 and time.time() - t < 900
def fail(k): FAILS[k] = (FAILS.get(k, (0, 0))[0] + 1, time.time())
def tick_soon():
    """Run escalation at most every 20s per process (efficiency)."""
    global LAST
    if time.time() - LAST > 20: LAST = time.time(); tick()
@app.before_request
def csrf_and_scheduler():
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Requested-With") != "SehatSaathi":
        return jsonify(error="Blocked: missing request header"), 403
    if not getattr(app, "_started", False) and not os.environ.get("NO_SCHEDULER"):
        app._started = True; init(); threading.Thread(target=loop, daemon=True).start()
@app.after_request
def secure(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
        "Permissions-Policy": "microphone=(self), geolocation=(self), camera=()",
        "Cache-Control": "no-store" if request.path.startswith("/api") else "public, max-age=86400" if request.path.startswith("/img/") else "no-cache"})
    return r

# ---------- auth ----------
# Every active member of the care circle, the patient included, can view, add, edit and manage the care plan.
# Only the patient controls who is in the circle (approve/remove) and records nothing as "missed" for themselves.
CAN = {"patient": {"manage", "task"}, "caregiver": {"manage", "task"}, "family": {"manage", "task"}}
def me(): return session.get("uid")
def need_login():
    if not me(): return jsonify(error="Sign in first"), 401
def role_for(pid):
    r = one("SELECT role FROM access WHERE user_id=? AND patient_id=? AND status='active'", (me(), pid)); return r["role"] if r else None
def guard(pid, perm=None):
    if not me(): return None, (jsonify(error="Sign in first"), 401)
    r = role_for(pid)
    if not r: return None, (jsonify(error="No access to this profile"), 403)
    if perm and perm not in CAN[r]: return None, (jsonify(error="Your role cannot do this"), 403)
    return r, None
def j(): return request.get_json(silent=True) or {}

@app.post("/api/register")
def register():
    b = j(); email = (b.get("email") or "").strip().lower()
    if "@" not in email or len(b.get("password", "")) < 8 or not b.get("name"):
        return jsonify(error="Name, valid email and an 8+ character password are required"), 400
    if one("SELECT 1 FROM users WHERE email=?", (email,)): return jsonify(error="Email already registered"), 409
    cur = db().execute("INSERT INTO users(email,name,pw) VALUES(?,?,?)", (email, b["name"], H(b["password"])))
    db().commit(); session.permanent = True; session["uid"] = cur.lastrowid
    return jsonify(ok=True)
@app.post("/api/login")
def login():
    b = j(); k = f"login:{request.remote_addr}:{(b.get('email') or '').lower()}"
    if limited(k): return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    u = one("SELECT * FROM users WHERE email=?", ((b.get("email") or "").lower(),))
    if not u or not C(u["pw"], b.get("password", "")): fail(k); return jsonify(error="Wrong email or password"), 401
    session.clear(); session.permanent = True; session["uid"] = u["id"]; return jsonify(ok=True)
@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)
@app.get("/api/me")
def whoami():
    if not me(): return jsonify(user=None)
    u = one("SELECT id,name,email FROM users WHERE id=?", (me(),))
    ps = rows("SELECT p.id,p.name,a.role FROM access a JOIN patients p ON p.id=a.patient_id WHERE a.user_id=? AND a.status='active'", (me(),))
    return jsonify(user=u, patients=ps, pending=rows("SELECT patient_id FROM access WHERE user_id=? AND status='pending'", (me(),)))
@app.post("/api/patients")  # family creates a shared profile + passcode; optionally patient's own login role
def create_patient():
    if not me(): return need_login()
    b = j()
    if not b.get("name") or len(b.get("passcode", "")) < 6: return jsonify(error="Patient name and a 6+ character passcode are required"), 400
    cur = db().execute("INSERT INTO patients(name,age,notes,passcode) VALUES(?,?,?,?)", (b["name"], b.get("age"), b.get("notes"), H(b["passcode"])))
    pid = cur.lastrowid; role = b.get("role") if b.get("role") in ("family", "patient") else "family"
    db().execute("INSERT INTO access(user_id,patient_id,role) VALUES(?,?,?)", (me(), pid, role)); audit(pid, "profile created", b["name"]); db().commit()
    return jsonify(id=pid)
@app.post("/api/join")  # passcode-based access; role chosen by joiner, consent = knowing the passcode set by family
def join():
    if not me(): return need_login()
    b = j(); p = one("SELECT * FROM patients WHERE id=?", (b.get("patient_id"),)); k = f"join:{me()}"
    if limited(k): return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    if b.get("consent") is not True: return jsonify(error="Confirm that the patient has agreed to share their health data"), 400
    if not p or not C(p["passcode"], b.get("passcode", "")) or b.get("role") not in ("patient", "family", "caregiver"):
        fail(k); return jsonify(error="Profile ID, passcode or role is incorrect"), 403
    if b["role"] == "patient" and members(p["id"], "patient"): return jsonify(error="Patient login already exists"), 409
    st = "pending" if b["role"] != "patient" and members(p["id"], "patient") else "active"  # patient approves new members
    db().execute("INSERT OR REPLACE INTO access(user_id,patient_id,role,status) VALUES(?,?,?,?)", (me(), p["id"], b["role"], st))
    audit(p["id"], "joined", f"{b['role']} ({st})"); db().commit()
    return jsonify(ok=True, status=st)

# ---------- patient data ----------
@app.get("/api/patients/<int:pid>/dashboard")
def dashboard(pid):
    role, err = guard(pid)
    if err: return err
    tick_soon()
    today = now().strftime("%Y-%m-%d"); d7 = (now() - timedelta(days=6)).strftime("%Y-%m-%d")
    meds = rows("SELECT * FROM medicines WHERE patient_id=? AND active=1 ORDER BY name", (pid,))
    for m in meds:
        per = len([h for h in (m["times"] or "").split(",") if h.strip()]) or 1
        m["days_left"] = None if m["qty"] is None else m["qty"] // per
    doses = rows("""SELECT d.*, m.name, m.dosage, m.instructions FROM doses d JOIN medicines m ON m.id=d.med_id
                    WHERE d.patient_id=? AND substr(d.due,1,10)=? ORDER BY d.due""", (pid, today))
    adh = rows("""SELECT substr(due,1,10) day, SUM(status='taken') taken, SUM(status='missed') missed,
                  SUM(status='unconfirmed') unconfirmed, SUM(status='skipped') skipped, SUM(status='upcoming') upcoming FROM doses
                  WHERE patient_id=? AND substr(due,1,10)>=? GROUP BY day ORDER BY day""", (pid, d7))
    pct = lambda tk, tot: round(100 * tk / tot) if tot else None  # share of finished doses that were taken; upcoming doses are not counted
    by_med = rows("""SELECT m.name, SUM(d.status='taken') taken,
                     SUM(d.status IN ('taken','missed','unconfirmed','skipped')) total FROM doses d JOIN medicines m ON m.id=d.med_id
                     WHERE d.patient_id=? AND substr(d.due,1,10)>=? GROUP BY m.id ORDER BY m.name""", (pid, d7))
    for m in by_med: m["pct"] = pct(m["taken"], m["total"])
    all_taken, all_total = sum(m["taken"] or 0 for m in by_med), sum(m["total"] or 0 for m in by_med)
    out = dict(role=role, patient=one("SELECT id,name,age,notes FROM patients WHERE id=?", (pid,)), meds=meds, doses=doses, adherence=adh, by_med=by_med, adherence_pct=pct(all_taken, all_total),
        appointments=rows("SELECT * FROM appointments WHERE patient_id=? ORDER BY when_at", (pid,)),
        tasks=rows("SELECT t.*, u.name assignee_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee WHERE patient_id=? ORDER BY done, due", (pid,)),
        notifications=rows("SELECT * FROM notifications WHERE patient_id=? AND user_id=? ORDER BY id DESC LIMIT 40", (pid, me())),
        team=rows("SELECT u.id,u.name,a.role,a.status FROM access a JOIN users u ON u.id=a.user_id WHERE a.patient_id=?", (pid,)))
    out["today"] = today; out["checkins"] = rows("SELECT * FROM checkins WHERE patient_id=? ORDER BY day DESC LIMIT 7", (pid,))
    out["audit"] = rows("SELECT a.*, u.name who FROM audit a LEFT JOIN users u ON u.id=a.user_id WHERE patient_id=? ORDER BY a.id DESC LIMIT 60", (pid,))  # the patient sees who did what
    return jsonify(out)

LABEL = {"medicines": "medicine", "appointments": "appointment", "tasks": "care task"}
def crud(table, fields, pid, body, defaults=None, perm="manage", rid=None):
    """Insert or update one row, record it in the activity log, and tell the rest of the care circle."""
    role, err = guard(pid, perm)
    if err: return err
    defaults = defaults or {}; vals = [body.get(f) if body.get(f) is not None else defaults.get(f) for f in fields]
    if rid is None:
        cur = db().execute(f"INSERT INTO {table}(patient_id,{','.join(fields)}) VALUES(?,{','.join('?'*len(fields))})", [pid] + vals); rid = cur.lastrowid; act = "added"
    else:
        if not one(f"SELECT 1 FROM {table} WHERE id=? AND patient_id=?", (rid, pid)): return jsonify(error="Not found"), 404
        db().execute(f"UPDATE {table} SET {','.join(f+'=?' for f in fields)} WHERE id=? AND patient_id=?", vals + [rid, pid]); act = "updated"
    audit(pid, f"{table} {act}", str(body.get(fields[0]))); db().commit()
    notify(pid, everyone_but_me(pid), "Care plan updated", f"{who()} {act} a {LABEL[table]}: {body.get(fields[0])}.", f"upd-{table}-{rid}-{me()}-{iso(now())}")
    tick(); return jsonify(id=rid)

HM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
def clean_times(s):
    """'8:00, 20:00' -> '08:00,20:00' (sorted, de-duplicated); None when any time is not a valid HH:MM."""
    ts = [x.strip() for x in str(s or "").split(",") if x.strip()]
    if not ts or not all(HM.match(x) for x in ts): return None
    return ",".join(sorted({"%02d:%s" % (int(HM.match(x).group(1)), HM.match(x).group(2)) for x in ts}))
def clean_day(s):
    if s in (None, ""): return None
    try: return datetime.strptime(str(s)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError: return False

@app.route("/api/patients/<int:pid>/medicines", methods=["POST"])
@app.route("/api/patients/<int:pid>/medicines/<int:rid>", methods=["PUT"])
def meds(pid, rid=None):
    b = dict(j()); times = clean_times(b.get("times"))
    if not str(b.get("name") or "").strip() or not str(b.get("dosage") or "").strip() or not b.get("times"): return jsonify(error="Name, dosage and at least one time (HH:MM) are required"), 400
    if not times: return jsonify(error="Times must look like 08:00 or 08:00,20:00"), 400
    start, end = clean_day(b.get("start")), clean_day(b.get("end"))
    if start is False or end is False or (start and end and end < start): return jsonify(error="Check the start and end dates"), 400
    try: qty = None if b.get("qty") in (None, "") else int(b["qty"])
    except (TypeError, ValueError): return jsonify(error="Stock must be a whole number"), 400
    if qty is not None and qty < 0: return jsonify(error="Stock cannot be negative"), 400
    b.update(times=times, start=start, end=end, qty=qty, name=b["name"].strip(), dosage=b["dosage"].strip())
    return crud("medicines", ["name", "dosage", "instructions", "times", "start", "end", "qty"], pid, b, rid=rid)
@app.delete("/api/patients/<int:pid>/medicines/<int:rid>")
def med_del(pid, rid):
    role, err = guard(pid, "manage")
    if err: return err
    m = one("SELECT name FROM medicines WHERE id=? AND patient_id=?", (rid, pid))
    if not m: return jsonify(error="Not found"), 404
    db().execute("UPDATE medicines SET active=0 WHERE id=? AND patient_id=?", (rid, pid))  # soft delete keeps history
    db().execute("DELETE FROM doses WHERE med_id=? AND status='upcoming'", (rid,)); audit(pid, "medicine deleted", m["name"]); db().commit()
    notify(pid, everyone_but_me(pid), "Care plan updated", f"{who()} removed a medicine: {m['name']}.", f"del-med-{rid}-{me()}-{iso(now())}")
    return jsonify(ok=True)
@app.route("/api/patients/<int:pid>/appointments", methods=["POST"])
@app.route("/api/patients/<int:pid>/appointments/<int:rid>", methods=["PUT"])
def appts(pid, rid=None):
    b = dict(j())
    if not str(b.get("doctor") or "").strip() or not b.get("when_at"): return jsonify(error="Doctor and date/time are required"), 400
    try: b["when_at"] = datetime.fromisoformat(str(b["when_at"])).strftime("%Y-%m-%dT%H:%M")
    except ValueError: return jsonify(error="Date and time are not valid"), 400
    if b.get("status") not in (None, "scheduled", "completed", "cancelled"): return jsonify(error="Status must be scheduled, completed or cancelled"), 400
    return crud("appointments", ["doctor", "speciality", "contact", "when_at", "place", "notes", "status"], pid, b, {"status": "scheduled"}, rid=rid)
@app.route("/api/patients/<int:pid>/tasks", methods=["POST"])
def tasks(pid):
    b = j()
    if not str(b.get("title") or "").strip(): return jsonify(error="Task title is required"), 400
    return crud("tasks", ["title", "assignee", "due"], pid, b)
@app.post("/api/patients/<int:pid>/tasks/<int:rid>/complete")
def task_done(pid, rid):
    role, err = guard(pid, "task")
    if err: return err
    db().execute("UPDATE tasks SET done=1, note=? WHERE id=? AND patient_id=?", (j().get("note"), rid, pid))
    audit(pid, "task completed", str(rid)); db().commit()
    notify(pid, members(pid, "family"), "Task update", f"A care task was completed (#{rid}).", f"task{rid}-done"); return jsonify(ok=True)

@app.post("/api/patients/<int:pid>/doses/<int:did>")
def dose_status(pid, did):
    role, err = guard(pid)
    if err: return err
    b = j(); st = b.get("status"); note = str(b.get("note") or "").strip()[:300]; x = one("SELECT * FROM doses WHERE id=? AND patient_id=?", (did, pid))
    if not x: return jsonify(error="Dose not found"), 404
    if st not in ("taken", "missed", "skipped"): return jsonify(error="Status must be taken, missed or skipped"), 400
    if st == "missed" and role == "patient": return jsonify(error="Only a caregiver or family member can record a missed dose"), 403
    if st == "skipped" and not note: return jsonify(error="Please say why the dose is skipped"), 400
    if x["status"] == "taken": return jsonify(error="This dose is already recorded as taken"), 409  # also stops stock being reduced twice
    if x["status"] == "upcoming" and st == "taken" and datetime.fromisoformat(x["due"]) - now() > timedelta(hours=1):
        return jsonify(error="This dose is not due yet"), 400
    db().execute("UPDATE doses SET status=?, by=?, note=?, at=? WHERE id=?", (st, me(), note or None, iso(now()), did))
    if st == "taken": db().execute("UPDATE medicines SET qty=qty-1 WHERE id=? AND qty>0", (x["med_id"],))
    audit(pid, f"dose {st}", f"#{did} by {role}" + (f": {note}" if note else "")); db().commit()
    if st != "taken" or role != "patient":  # the patient taking their own dose needs no alert; everything else is shared with the circle
        label = {"taken": "taken", "missed": "missed", "skipped": "skipped by choice"}[st]
        notify(pid, everyone_but_me(pid), "Dose update", f"{who()} ({role}) recorded a dose as {label}. {note}".strip(), f"dose{did}-rec-{st}")
    return jsonify(ok=True)
@app.post("/api/notifications/<int:nid>/read")
def read(nid):
    if not me(): return need_login()
    db().execute("UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (nid, me())); db().commit(); return jsonify(ok=True)
@app.post("/api/patients/<int:pid>/access/<int:uid>/approve")
def approve(pid, uid):
    role, err = guard(pid)
    if err: return err
    if role != "patient": return jsonify(error="Only the patient can approve access"), 403
    db().execute("UPDATE access SET status='active' WHERE user_id=? AND patient_id=?", (uid, pid)); audit(pid, "access approved", str(uid)); db().commit()
    return jsonify(ok=True)
@app.delete("/api/patients/<int:pid>/access/<int:uid>")
def revoke(pid, uid):
    role, err = guard(pid)
    if err: return err
    if (role != "patient" and uid != me()) or (role == "patient" and uid == me()): return jsonify(error="Only the patient can remove others; you cannot remove yourself"), 403
    db().execute("DELETE FROM access WHERE user_id=? AND patient_id=?", (uid, pid)); audit(pid, "access removed", str(uid)); db().commit()
    return jsonify(ok=True)
@app.post("/api/patients/<int:pid>/passcode")
def change_passcode(pid):
    """The patient decides who can join: only they (or family, if the patient has no login) can set a new passcode."""
    role, err = guard(pid)
    if err: return err
    if role != ("patient" if members(pid, "patient") else "family"): return jsonify(error="Only the patient can change the passcode"), 403
    pc = str(j().get("passcode") or "")
    if len(pc) < 6: return jsonify(error="Passcode must be 6+ characters"), 400
    db().execute("UPDATE patients SET passcode=? WHERE id=?", (H(pc), pid)); audit(pid, "passcode changed"); db().commit()
    return jsonify(ok=True)
@app.post("/api/test-email")
def test_email():
    """Lets a user confirm that reminder emails can reach them."""
    if not me(): return need_login()
    st = send_email(one("SELECT email FROM users WHERE id=?", (me(),))["email"], "Sehat Saathi: test email", "If you can read this, medicine and appointment reminders will reach you.")
    return jsonify(status=st)
@app.post("/api/patients/<int:pid>/checkin")
def checkin(pid):
    role, err = guard(pid)
    if err: return err
    b = j(); ok = {"feeling": ("good", "okay", "unwell"), "sleep": ("yes", "no"), "meals": ("yes", "no"), "help": ("yes", "no")}
    if any(b.get(k) not in v for k, v in ok.items()): return jsonify(error="Please answer all four questions"), 400
    day = now().strftime("%Y-%m-%d")
    db().execute("INSERT OR REPLACE INTO checkins VALUES(?,?,?,?,?,?,?)", (pid, day, b["feeling"], b["sleep"], b["meals"], b["help"], me()))
    audit(pid, "wellbeing check-in", f"feeling {b['feeling']}, help {b['help']}"); db().commit()
    if b["help"] == "yes" or b["feeling"] == "unwell":
        notify(pid, members(pid, "caregiver") + members(pid, "family"), "Needs assistance", f"Check-in on {day}: feeling {b['feeling']}, needs help: {b['help']}.", f"checkin{pid}-{day}")
    return jsonify(ok=True)
@app.post("/api/patients/<int:pid>/sos")
def sos(pid):
    role, err = guard(pid)
    if err: return err
    b = j()
    try: loc = f" Location: https://www.google.com/maps?q={float(b['lat'])},{float(b['lng'])}" if b.get("lat") is not None else " Location was not shared."
    except (TypeError, ValueError): return jsonify(error="Invalid location"), 400
    ids = members(pid, "family") + members(pid, "caregiver")
    notify(pid, ids, "Emergency alert", f"EMERGENCY alert from {one('SELECT name FROM patients WHERE id=?', (pid,))['name']}.{loc}", f"sos{pid}-{iso(now())}")
    audit(pid, "SOS pressed", f"{len(ids)} contacts alerted"); db().commit()
    return jsonify(recipients=len(ids), emergency_services_contacted=False)
@app.get("/api/patients/<int:pid>/summary")
def summary(pid):
    role, err = guard(pid)
    if err: return err
    text, source = build_summary(pid); return jsonify(text=text, source=source)
@app.post("/api/patients/<int:pid>/ask")
def ask(pid):
    """Talk to the assistant (text from voice or keyboard). Answers come from recorded data only."""
    role, err = guard(pid)
    if err: return err
    tick_soon(); today = now().strftime("%Y-%m-%d")
    doses = rows("SELECT d.id,d.due,d.status,m.name,m.dosage FROM doses d JOIN medicines m ON m.id=d.med_id WHERE d.patient_id=? AND substr(d.due,1,10)=? ORDER BY d.due", (pid, today))
    appt = one("SELECT doctor,when_at FROM appointments WHERE patient_id=? AND status='scheduled' AND when_at>=? ORDER BY when_at LIMIT 1", (pid, iso(now())))
    out = assistant.reply(str(j().get("text", ""))[:300], doses, appt, lambda: build_summary(pid)[0])
    audit(pid, "assistant question", out["intent"]); db().commit(); return jsonify(out)
def build_summary(pid):
    """Daily care summary from recorded data; optionally reworded by Claude when ANTHROPIC_API_KEY is set."""
    tick_soon(); t = now(); today = t.strftime("%Y-%m-%d"); d7 = (t - timedelta(days=6)).strftime("%Y-%m-%d")
    ds = rows("SELECT status FROM doses WHERE patient_id=? AND substr(due,1,10)=?", (pid, today)); c = lambda s: sum(d["status"] == s for d in ds)
    f = [f"{len(ds)} medicine doses are scheduled today: {c('taken')} confirmed taken, {c('unconfirmed')} not yet confirmed, {c('upcoming')} upcoming, {c('missed')} recorded as missed, {c('skipped')} skipped by choice."]
    for r in rows("SELECT m.name, COUNT(*) n FROM doses d JOIN medicines m ON m.id=d.med_id WHERE d.patient_id=? AND d.status IN ('unconfirmed','missed') AND substr(d.due,1,10)>=? GROUP BY m.name HAVING n>=2", (pid, d7)):
        f.append(f"{r['name']} was unconfirmed or missed {r['n']} times in the last 7 days. A follow-up with the caregiver or doctor may help.")
    a = one("SELECT * FROM appointments WHERE patient_id=? AND status='scheduled' AND when_at>=? ORDER BY when_at LIMIT 1", (pid, iso(t)))
    if a: f.append(f"Next appointment: Dr {a['doctor']} on {a['when_at'].replace('T', ' ')} at {a['place'] or 'no location set'}.")
    tk = rows("SELECT title FROM tasks WHERE patient_id=? AND done=0", (pid,))
    if tk: f.append(f"{len(tk)} care task(s) pending: " + ", ".join(x["title"] for x in tk) + ".")
    for m in rows("SELECT name, qty, times FROM medicines WHERE patient_id=? AND active=1 AND qty IS NOT NULL", (pid,)):
        left = m["qty"] // (len([h for h in m["times"].split(",") if h.strip()]) or 1)
        if left <= 5: f.append(f"{m['name']} has about {left} days of stock left.")
    ci = one("SELECT * FROM checkins WHERE patient_id=? AND day=?", (pid, today))
    f.append(f"Today's check-in: feeling {ci['feeling']}, needs help: {ci['help']}." if ci else "No wellbeing check-in has been recorded today.")
    text, source = " ".join(f), "records"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            body = {"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"), "max_tokens": 400, "system": "Rewrite these care facts as a warm, plain 3-5 sentence summary for a family. Use only the facts given. Never diagnose, prescribe, change doses or give medical advice.", "messages": [{"role": "user", "content": text}]}
            rq = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(), {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            with urllib.request.urlopen(rq, timeout=15) as r: text, source = json.load(r)["content"][0]["text"], "ai"
        except Exception as e: source = "records (AI unavailable)"
    return text, source
@app.get("/")
def index(): return app.send_static_file("index.html")

if __name__ == "__main__":
    init()
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
