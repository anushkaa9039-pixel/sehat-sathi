"""Synthetic demo data only. Run: python seed.py"""
import os, sqlite3, random
from datetime import timedelta
os.environ.setdefault("DB_PATH", "care.db")
import app as A
from werkzeug.security import generate_password_hash as H
if os.path.exists(A.DB): os.remove(A.DB)
A.init(); c = sqlite3.connect(A.DB); n = A.now()
for i, (e, nm) in enumerate([("asha@demo.test", "Asha (Daughter)"), ("ravi@demo.test", "Ravi (Caregiver)"), ("kamla@demo.test", "Kamla Devi (Patient)")], 1):
    c.execute("INSERT INTO users VALUES(?,?,?,?)", (i, e, nm, H("Demo@1234")))
c.execute("INSERT INTO patients VALUES(1,'Kamla Devi',74,'Type 2 diabetes, high BP. Lives alone.',?)", (H("family-pass-1"),))
c.executemany("INSERT INTO access(user_id,patient_id,role) VALUES(?,1,?)", [(1, "family"), (2, "caregiver"), (3, "patient")])
d = lambda m: (n + timedelta(minutes=m)).strftime("%H:%M")
meds = [("Metformin", "500 mg", "After breakfast", d(-45), 40), ("Amlodipine", "5 mg", "Once daily", d(-200), 12), ("Vitamin D3", "1000 IU", "With lunch", d(120), 30)]
for i, (nm, ds, ins, t, q) in enumerate(meds, 1):
    c.execute("INSERT INTO medicines VALUES(?,1,?,?,?,?,?,NULL,?,1)", (i, nm, ds, ins, t, (n - timedelta(days=30)).strftime("%Y-%m-%d"), q))
    for back in range(1, 7):  # six days of history
        st = random.Random(i * back).choice(["taken"] * 6 + ["missed", "unconfirmed"])
        c.execute("INSERT INTO doses(med_id,patient_id,due,status) VALUES(?,1,?,?)", (i, (n - timedelta(days=back)).strftime("%Y-%m-%dT") + t, st))
c.execute("INSERT INTO appointments(patient_id,doctor,speciality,contact,when_at,place,notes) VALUES(1,'Meera Singh','Endocrinologist','+91-90000-00000',?,'City Clinic, Patna','Bring HbA1c report')", ((n + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M"),))
c.execute("INSERT INTO tasks(patient_id,title,assignee,due) VALUES(1,'Collect Metformin refill',2,?)", ((n + timedelta(days=1)).strftime("%Y-%m-%d"),))
c.commit(); print("Seeded. Logins (password Demo@1234): asha@demo.test, ravi@demo.test, kamla@demo.test")
