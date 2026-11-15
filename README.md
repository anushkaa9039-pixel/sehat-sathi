# Sehat Saathi: Elderly Care & Medication Management Platform

A shared care space where an elderly person, their caretakers and their family manage medicines and doctor visits together,
with the elderly person in control. Flask + SQLite + a single accessible HTML page.

## Run
```
pip install -r requirements.txt
python seed.py && python app.py     # open http://127.0.0.1:5000
pytest -q                           # 39 tests
```
Demo logins (synthetic, password `Demo@1234`): `asha@demo.test` (family), `ravi@demo.test` (caregiver), `kamla@demo.test` (patient).
Profile 1 passcode: `family-pass-1`.

## How it meets the challenge
| Challenge requirement | Where it is built |
|---|---|
| Elderly individuals manage daily medications, appointments and routines | Patient login sees today's doses as large cards ("I took it", "Skip this dose" with a reason), can add/edit medicines and appointments, daily wellbeing check-in, voice assistant (Hindi/English/Hinglish) |
| Keep family and caretakers informed | Every action by one member (dose taken/missed/skipped, medicine or appointment added/edited/removed, task done, SOS, check-in needing help) is sent to the rest of the circle as an in-app alert and an email |
| Shared ecosystem for patient, caretakers, family | Roles `patient`, `caregiver`, `family` on one profile; activity log shows who changed what, visible to everyone including the patient |
| Secure, passcode-based access | Family creates a profile and passcode; others join with profile ID + passcode + consent; hashed passcodes; login/passcode rate limiting; CSRF header, security headers; the patient can rotate the passcode |
| Authorized members can view, add, edit and manage medication schedules | Medicine CRUD for every active member, with validation (HH:MM times, dates, stock) and soft delete that keeps history |
| Track adherence | Dose statuses (taken / not confirmed / missed / skipped), per-day chart, 7-day adherence % overall and per medicine, refill warnings from stock |
| Monitor upcoming doctor appointments | Appointment CRUD with status (scheduled/completed/cancelled), next appointment on the Today page |
| Proactively send email reminders for **upcoming doses** | Reminder 10 minutes before each dose |
| ...for **missed medications** | Escalation after the due time: patient reminder, then caregiver ("possible missed dose"), then family. Also an alert whenever someone records a dose as missed |
| ...for **medical appointments** | Reminder 24 hours and again 2 hours before; a rescheduled appointment is announced again; cancelled ones send nothing |
| Go beyond a basic reminder app | Escalation ladder, daily care summary, wellbeing check-in, SOS with location, voice assistant that answers only from recorded data, refill prediction, official drug-label links, caregiver tasks |
| More accessible and reliable | 20px base type, 56px+ touch targets, "A+ Larger text" toggle, skip link, labelled dialogs, visible focus, reduced-motion support, hidden card faces removed from keyboard order. Emails are queued and sent in the background with up to 3 attempts; duplicate alerts are prevented |
| Elderly remain at the centre of their own healthcare decisions | The patient approves or removes anyone in the circle, controls the passcode, can skip a dose by choice (shared with the circle, never auto-labelled "missed"), sees the full activity log and adherence report |

## Email setup
Reminders are always stored in the in-app Alerts tab. To also send email, set SMTP variables (Gmail works with an app password):
```
SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com SMTP_PASS=<app password> SMTP_FROM=you@gmail.com
```
Each alert shows its email status (`queued`, `sent`, `failed: ...` or `not_configured`); nothing is reported as sent unless it was.
The Alerts tab has a **Send me a test email** button to check the setup.

## Environment
`SECRET_KEY` (set in production), `DB_PATH`, `ESCALATION_MIN` (default `0,15,30,60` minutes after the due time: patient notified, reminder + marked unconfirmed, caregiver, family),
`COOKIE_SECURE=1` behind HTTPS, `ANTHROPIC_API_KEY` (optional; rewords the summary and answers free-form questions from recorded facts only. Untested here, no network).

## Safety notes
An unconfirmed dose is never recorded as missed automatically; only a person can do that. The app gives no dosing or diagnosis advice
(the assistant refuses such questions). SOS alerts contacts and says plainly that it does not call emergency services.
Demo medicines link to their official DailyMed labels. Voice input needs Chrome or Edge.

## Deploy
`Dockerfile` or `Procfile` (gunicorn, one worker so the reminder scheduler runs once). CI runs `pytest` on every push.
