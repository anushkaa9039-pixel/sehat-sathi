import os
os.environ.update(DB_PATH="test_pytest.db", NO_SCHEDULER="1")
import pytest, seed, app as A
def client(email=None):
    c = A.app.test_client(); c.environ_base["HTTP_X_REQUESTED_WITH"] = "SehatSaathi"
    if email: assert c.post("/api/login", json={"email": email, "password": "Demo@1234"}).status_code == 200
    return c
@pytest.fixture(autouse=True)
def fresh():
    seed_db = A.DB; A.LAST = 0; A.FAILS.clear(); yield
D = lambda c: c.get("/api/patients/1/dashboard").get_json()
def test_requires_login(): assert client().get("/api/patients/1/dashboard").status_code == 401
def test_csrf_header_required():
    c = A.app.test_client(); assert c.post("/api/login", json={}).status_code == 403
def test_security_headers():
    h = client().get("/").headers; assert h["X-Frame-Options"] == "DENY" and "frame-ancestors" in h["Content-Security-Policy"]
def test_escalation_reaches_caregiver_then_family():
    assert any(n["kind"] == "Possible missed dose" for n in D(client("ravi@demo.test"))["notifications"])
    assert any(n["kind"] == "Missed dose unresolved" for n in D(client("asha@demo.test"))["notifications"])
def test_no_duplicate_alerts():
    f = client("asha@demo.test"); a = len(D(f)["notifications"]); A.LAST = 0; assert len(D(f)["notifications"]) == a
def test_email_status_is_honest(): assert D(client("asha@demo.test"))["notifications"][0]["email"] in ("not_configured", "sent")
def test_patient_cannot_record_missed():
    p = client("kamla@demo.test"); d = [x for x in D(p)["doses"] if x["status"] == "unconfirmed"][0]
    assert p.post(f"/api/patients/1/doses/{d['id']}", json={"status": "missed"}).status_code == 403
def test_every_active_member_can_manage_medicines_but_outsiders_cannot():
    for who in ("ravi@demo.test", "kamla@demo.test", "asha@demo.test"):  # caregiver, patient, family
        assert client(who).post("/api/patients/1/medicines", json={"name": "Test " + who[:3], "dosage": "1", "times": "09:00"}).status_code == 200
    c = client(); c.post("/api/register", json={"name": "Out", "email": "out@x.test", "password": "password123"})
    assert c.post("/api/patients/1/medicines", json={"name": "x", "dosage": "1", "times": "09:00"}).status_code == 403
def test_outsider_blocked():
    c = client(); c.post("/api/register", json={"name": "Eve", "email": "eve@x.test", "password": "password123"})
    assert c.get("/api/patients/1/dashboard").status_code == 403
def test_join_needs_consent_and_passcode():
    c = client(); c.post("/api/register", json={"name": "Sam", "email": "sam@x.test", "password": "password123"})
    body = {"patient_id": 1, "passcode": "family-pass-1", "role": "caregiver"}
    assert c.post("/api/join", json=body).status_code == 400
    assert c.post("/api/join", json={**body, "consent": True, "passcode": "wrong"}).status_code == 403
    assert c.post("/api/join", json={**body, "consent": True}).status_code == 200
def test_login_lockout():
    c = client()
    for _ in range(5): assert c.post("/api/login", json={"email": "lock@x.test", "password": "bad"}).status_code == 401
    assert c.post("/api/login", json={"email": "lock@x.test", "password": "bad"}).status_code == 429
def test_taken_updates_family_view_and_stock():
    p, f = client("kamla@demo.test"), client("asha@demo.test"); before = D(f)
    d = [x for x in before["doses"] if x["status"] == "unconfirmed"][0]
    assert p.post(f"/api/patients/1/doses/{d['id']}", json={"status": "taken"}).status_code == 200
    assert [x for x in D(f)["doses"] if x["id"] == d["id"]][0]["status"] == "taken"
def test_unconfirmed_never_auto_missed():
    assert not [d for d in D(client("asha@demo.test"))["doses"] if d["status"] == "missed" and not d["by"]]
def test_validation_rejects_bad_medicine():
    assert client("asha@demo.test").post("/api/patients/1/medicines", json={"name": "x"}).status_code == 400
def test_appointment_created():
    f = client("asha@demo.test"); assert f.post("/api/patients/1/appointments", json={"doctor": "Rao", "when_at": "2030-01-01T10:00"}).status_code == 200
    assert any(a["doctor"] == "Rao" for a in D(f)["appointments"])
def _joiner(email, role="caregiver"):
    c = client(); c.post("/api/register", json={"name": "New", "email": email, "password": "password123"})
    r = c.post("/api/join", json={"patient_id": 1, "passcode": "family-pass-1", "role": role, "consent": True}); return c, r.get_json()
def test_new_member_needs_patient_approval_and_can_be_removed():
    c, r = _joiner("n1@x.test"); assert r["status"] == "pending" and c.get("/api/patients/1/dashboard").status_code == 403
    uid = c.get("/api/me").get_json()["user"]["id"]; p = client("kamla@demo.test")
    assert client("asha@demo.test").post(f"/api/patients/1/access/{uid}/approve").status_code == 403
    assert p.post(f"/api/patients/1/access/{uid}/approve").status_code == 200 and c.get("/api/patients/1/dashboard").status_code == 200
    assert p.delete(f"/api/patients/1/access/{uid}").status_code == 200 and c.get("/api/patients/1/dashboard").status_code == 403
def test_checkin_help_alerts_family():
    p = client("kamla@demo.test"); assert p.post("/api/patients/1/checkin", json={"feeling": "unwell", "sleep": "no", "meals": "yes", "help": "yes"}).status_code == 200
    assert any(n["kind"] == "Needs assistance" for n in D(client("asha@demo.test"))["notifications"])
    assert p.post("/api/patients/1/checkin", json={"feeling": "good"}).status_code == 400
def test_sos_alerts_contacts_and_is_honest():
    r = client("kamla@demo.test").post("/api/patients/1/sos", json={"lat": 25.59, "lng": 85.13}).get_json()
    assert r["recipients"] >= 2 and r["emergency_services_contacted"] is False
    assert any(n["kind"] == "Emergency alert" and "maps?q=25.59" in n["msg"] for n in D(client("ravi@demo.test"))["notifications"])
def test_summary_is_built_from_records():
    s = client("ravi@demo.test").get("/api/patients/1/summary").get_json(); assert s["source"].startswith("records") and "not yet confirmed" in s["text"]
def test_dose_reminder_sent_before_due():
    from datetime import datetime, timedelta
    d = [x for x in D(client("kamla@demo.test"))["doses"] if x["status"] == "upcoming"][0]
    with A.app.app_context(): A.tick(datetime.fromisoformat(d["due"]) - timedelta(minutes=5))
    assert any(n["kind"] == "Upcoming dose" for n in D(client("kamla@demo.test"))["notifications"])
def test_assistant_understands_hinglish_and_refuses_advice():
    import assistant as S
    assert S.intent("Maine apni medicine le li hai") == "taken" and S.language("Doctor appointment kab hai?") == "hi"
    r = S.reply("kya main dose double kar loon", [], None, lambda: "")
    assert r["intent"] == "advice" and "doctor" in r["reply"].lower() and r["action"] is None
def test_ask_requires_confirmation_for_taken_and_never_marks_by_itself():
    p = client("kamla@demo.test"); r = p.post("/api/patients/1/ask", json={"text": "Maine medicine le li"}).get_json()
    assert r["action"]["type"] == "confirm_dose"
    assert [d for d in D(p)["doses"] if d["id"] == r["action"]["dose_id"]][0]["status"] != "taken"
def test_ask_answers_from_records_and_needs_access():
    assert "Metformin" in client("ravi@demo.test").post("/api/patients/1/ask", json={"text": "What medicines do I need today?"}).get_json()["reply"]
    assert client().post("/api/patients/1/ask", json={"text": "hi"}).status_code in (401, 403)

# ---------- problem-statement alignment tests (each uses its own profile so they do not depend on the demo data) ----------
def _circle(tag):
    """A fresh profile with a family, a caregiver and a patient login, all active."""
    fam = client(); fam.post("/api/register", json={"name": "Fam " + tag, "email": f"fam-{tag}@x.test", "password": "password123"})
    pid = fam.post("/api/patients", json={"name": "Elder " + tag, "passcode": "secret-" + tag}).get_json()["id"]
    out = {"fam": fam, "pid": pid}
    for role in ("patient", "caregiver"):
        c = client(); c.post("/api/register", json={"name": f"{role} {tag}", "email": f"{role}-{tag}@x.test", "password": "password123"})
        r = c.post("/api/join", json={"patient_id": pid, "passcode": "secret-" + tag, "role": role, "consent": True}).get_json()
        if r["status"] == "pending":  # the patient approves later joiners
            uid = c.get("/api/me").get_json()["user"]["id"]; assert out["patient"].post(f"/api/patients/{pid}/access/{uid}/approve").status_code == 200
        out[role] = c
    return out
def _past_med(c, pid, minutes_ago=90, **kw):
    t = (A.now() - __import__("datetime").timedelta(minutes=minutes_ago)).strftime("%H:%M")
    assert c.post(f"/api/patients/{pid}/medicines", json={"name": "Med", "dosage": "1 tab", "times": t, **kw}).status_code == 200
    with A.app.app_context(): A.tick()
def _dash(c, pid): return c.get(f"/api/patients/{pid}/dashboard").get_json()

def test_patient_manages_own_care_and_sees_adherence_and_activity_log():
    k = _circle("a"); pid = k["pid"]
    assert k["patient"].post(f"/api/patients/{pid}/appointments", json={"doctor": "Own", "when_at": "2031-05-01T10:00"}).status_code == 200
    d = _dash(k["patient"], pid)
    assert d["appointments"][0]["status"] == "scheduled" and "adherence_pct" in d and d["audit"]  # default status, adherence and log visible to the patient
    assert any(n["kind"] == "Care plan updated" for n in _dash(k["fam"], pid)["notifications"])  # family is told about the patient's change
def test_patient_approval_and_passcode_rotation():
    k = _circle("b"); pid = k["pid"]
    assert k["fam"].post(f"/api/patients/{pid}/passcode", json={"passcode": "newpass-b"}).status_code == 403  # a patient exists, so only they can rotate it
    assert k["patient"].post(f"/api/patients/{pid}/passcode", json={"passcode": "123"}).status_code == 400
    assert k["patient"].post(f"/api/patients/{pid}/passcode", json={"passcode": "newpass-b"}).status_code == 200
    c = client(); c.post("/api/register", json={"name": "Late", "email": "late-b@x.test", "password": "password123"})
    body = {"patient_id": pid, "role": "family", "consent": True}
    assert c.post("/api/join", json={**body, "passcode": "secret-b"}).status_code == 403  # old passcode no longer works
    assert c.post("/api/join", json={**body, "passcode": "newpass-b"}).status_code == 200
def test_medicine_validation_and_time_normalising():
    f = client("asha@demo.test"); url = "/api/patients/1/medicines"; ok = {"name": "V", "dosage": "1"}
    assert f.post(url, json={**ok, "times": "25:00"}).status_code == 400
    assert f.post(url, json={**ok, "times": "08:00", "qty": "abc"}).status_code == 400
    assert f.post(url, json={**ok, "times": "08:00", "qty": -3}).status_code == 400
    assert f.post(url, json={**ok, "times": "08:00", "start": "2030-02-01", "end": "2030-01-01"}).status_code == 400
    assert f.post(url, json={"name": "Norm", "dosage": "1", "times": "8:00, 20:30,8:00", "qty": "12"}).status_code == 200
    assert [m for m in D(f)["meds"] if m["name"] == "Norm"][0]["times"] == "08:00,20:30"
def test_appointment_rejects_bad_datetime_and_gets_scheduled_status():
    f = client("asha@demo.test"); url = "/api/patients/1/appointments"
    assert f.post(url, json={"doctor": "X", "when_at": "not-a-date"}).status_code == 400
    assert f.post(url, json={"doctor": "Gamma", "when_at": "2032-01-01T09:00", "status": "bogus"}).status_code == 400
    assert f.post(url, json={"doctor": "Gamma", "when_at": "2032-01-01T09:00"}).status_code == 200
    assert [a for a in D(f)["appointments"] if a["doctor"] == "Gamma"][0]["status"] == "scheduled"
def test_appointment_reminders_24h_2h_and_reschedule():
    from datetime import timedelta
    k = _circle("c"); pid = k["pid"]; soon = (A.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    r = k["fam"].post(f"/api/patients/{pid}/appointments", json={"doctor": "Soon", "when_at": soon}).get_json()
    kinds = [n["kind"] for n in _dash(k["patient"], pid)["notifications"]]
    assert "Appointment" in kinds and "Appointment soon" in kinds
    later = (A.now() + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    k["fam"].put(f"/api/patients/{pid}/appointments/{r['id']}", json={"doctor": "Soon", "when_at": later})
    assert [n["kind"] for n in _dash(k["patient"], pid)["notifications"]].count("Appointment") == 2  # rescheduling is announced again
    k["fam"].put(f"/api/patients/{pid}/appointments/{r['id']}", json={"doctor": "Soon", "when_at": later, "status": "cancelled"})
    n_before = len(_dash(k["patient"], pid)["notifications"]); A.LAST = 0
    with A.app.app_context(): A.tick()
    assert len(_dash(k["patient"], pid)["notifications"]) == n_before  # cancelled appointments send no reminders
def test_skipping_a_dose_is_the_patients_choice_needs_a_reason_and_stops_escalation():
    k = _circle("d"); pid = k["pid"]; _past_med(k["fam"], pid)
    dose = [x for x in _dash(k["patient"], pid)["doses"] if x["status"] in ("upcoming", "unconfirmed")][0]
    assert k["patient"].post(f"/api/patients/{pid}/doses/{dose['id']}", json={"status": "skipped"}).status_code == 400
    assert k["patient"].post(f"/api/patients/{pid}/doses/{dose['id']}", json={"status": "skipped", "note": "Feeling sick after food"}).status_code == 200
    assert [x for x in _dash(k["fam"], pid)["doses"] if x["id"] == dose["id"]][0]["status"] == "skipped"
    assert any("skipped" in n["msg"] for n in _dash(k["caregiver"], pid)["notifications"])
    assert _dash(k["patient"], pid)["adherence_pct"] == 0
def test_taken_twice_does_not_reduce_stock_twice_and_adherence_is_calculated():
    k = _circle("e"); pid = k["pid"]; _past_med(k["caregiver"], pid, qty=10)
    dose = _dash(k["patient"], pid)["doses"][0]; url = f"/api/patients/{pid}/doses/{dose['id']}"
    assert k["patient"].post(url, json={"status": "taken"}).status_code == 200
    assert k["patient"].post(url, json={"status": "taken"}).status_code == 409
    d = _dash(k["fam"], pid); assert d["meds"][0]["qty"] == 9 and d["adherence_pct"] == 100 and d["by_med"][0]["pct"] == 100
def test_missed_dose_is_shared_with_the_patient_and_family():
    k = _circle("f"); pid = k["pid"]; _past_med(k["fam"], pid)
    dose = [x for x in _dash(k["patient"], pid)["doses"] if x["status"] != "taken"][0]
    assert k["caregiver"].post(f"/api/patients/{pid}/doses/{dose['id']}", json={"status": "missed", "note": "Was asleep"}).status_code == 200
    for who in ("patient", "fam"): assert any("missed" in n["msg"] and n["kind"] == "Dose update" for n in _dash(k[who], pid)["notifications"])
def test_emails_are_queued_sent_in_background_and_retried(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid"); sent, mode = [], {"fail": True}
    def fake(to, subject, body):
        if mode["fail"]: return "failed: down"
        sent.append((to, subject)); return "sent"
    monkeypatch.setattr(A, "send_email", fake)
    k = _circle("g"); pid = k["pid"]; _past_med(k["fam"], pid, minutes_ago=40)  # 40 min late: patient, then caregiver, are alerted
    assert any(n["email"] == "queued" for n in _dash(k["patient"], pid)["notifications"])  # the request itself never waits for SMTP
    with A.app.app_context(): A.flush_emails()
    assert not sent and any(n["email"].startswith("failed") for n in _dash(k["patient"], pid)["notifications"])
    mode["fail"] = False
    with A.app.app_context(): A.flush_emails()
    assert sent and all(n["email"] == "sent" for n in _dash(k["patient"], pid)["notifications"])
    assert any("Missed" in s or "missed" in s for _, s in sent)
def test_email_gives_up_after_three_attempts(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid"); calls = []
    monkeypatch.setattr(A, "send_email", lambda *a: calls.append(a) or "failed: down")
    k = _circle("h"); _past_med(k["fam"], k["pid"], minutes_ago=5)
    for _ in range(6):
        with A.app.app_context(): A.flush_emails()
    import sqlite3; db = sqlite3.connect(A.DB)
    tries = [r[0] for r in db.execute("SELECT tries FROM notifications WHERE patient_id=?", (k["pid"],))]
    assert tries and set(tries) == {3} and len(calls) == 3 * len(tries)  # every email was attempted exactly three times, then dropped
def test_test_email_endpoint(monkeypatch):
    assert client().post("/api/test-email").status_code == 401
    assert client("asha@demo.test").post("/api/test-email").get_json()["status"] in ("not_configured", "sent")

def test_assistant_answers_from_recorded_data():
    r = client("kamla@demo.test").post("/api/patients/1/ask", json={"text": "Aaj ki dawa kaun si hai?"}).get_json()
    assert r["intent"] == "medicines" and "Amlodipine" in r["reply"]
def test_assistant_refuses_dose_advice():
    r = client("kamla@demo.test").post("/api/patients/1/ask", json={"text": "should I double the dose?"}).get_json()
    assert r["intent"] == "advice" and "doctor" in r["reply"].lower()
def test_assistant_needs_login_and_membership():
    assert client().post("/api/patients/1/ask", json={"text": "hi"}).status_code == 401
def test_activity_log_has_creator_and_profile_name_separately():
    c = client(); c.post("/api/register", json={"name": "Anushka", "email": "anu@x.test", "password": "password123"})
    pid = c.post("/api/patients", json={"name": "Anushka", "age": 70, "passcode": "secret12"}).get_json()["id"]
    e = c.get(f"/api/patients/{pid}/dashboard").get_json()["audit"][0]
    assert e["action"] == "profile created" and e["who"] == "Anushka" and e["detail"] == "Anushka"
def test_headers_and_static_images():
    c = client(); h = c.get("/img/art-today.jpg"); assert h.status_code == 200 and "max-age" in h.headers["Cache-Control"]
    r = c.get("/"); assert "microphone=(self)" in r.headers["Permissions-Policy"] and "object-src 'none'" in r.headers["Content-Security-Policy"]
    assert c.get("/api/me").headers["Cache-Control"] == "no-store"
