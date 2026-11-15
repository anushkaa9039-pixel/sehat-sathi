"""Voice/text assistant. Understands Hindi, English and Hinglish; answers only from the patient's recorded data.
It never gives dosing or diagnosis advice, and sensitive actions are returned as a request for confirmation."""
import json, os, re, urllib.request

HINDI = re.compile(r"[\u0900-\u097F]|\b(maine|meri|mujhe|dawa|davai|kab|aaj|kya|hai|batao|bata|le li|liya)\b", re.I)
RULES = [("advice", r"double|dugni|dugna|badha|kam kar|diagnos|bimari kya|kya khaun|band kar|stop taking"),
         ("taken", r"le li|le liya|le chuka|kha li|kha liya|i took|i have taken|\btaken\b|ले ली|ले लिया"),
         ("appointment", r"appointment|doctor|daktar|डॉक्टर|kab hai"),
         ("call", r"\bcall\b|phone|bulao|फोन"),
         ("summary", r"summary|kaisa|how am i|haal|सारांश"),
         ("medicines", r"medicine|dawa|davai|दवा|tablet|goli|today|aaj|आज|schedule")]

def language(text): return "hi" if HINDI.search(text) else "en"
def intent(text): return next((n for n, p in RULES if re.search(p, text.lower())), "unknown")

def ask_llm(text, facts):
    """Optional: free-form answers from Claude when ANTHROPIC_API_KEY is set (sends the facts to the API)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key: return None
    body = {"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"), "max_tokens": 250,
            "system": "You talk with an elderly patient. Answer in 1-3 short sentences, in the user's language (Hindi, English or Hinglish), using only the facts given. Never diagnose, prescribe or change doses; for such questions say to ask the doctor.",
            "messages": [{"role": "user", "content": f"Facts: {facts}\nQuestion: {text}"}]}
    try:
        rq = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(),
                                    {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(rq, timeout=15) as r: return json.load(r)["content"][0]["text"]
    except Exception: return None

def reply(text, doses, appt, summary):
    """Return {reply, lang, intent, action}. `summary` is a callable so it only runs when needed."""
    L, i, action = language(text), intent(text), None
    pick = lambda en, hi: hi if L == "hi" else en
    if i == "advice":
        r = pick("I can't advise on doses or conditions. Please ask your doctor or pharmacist. Never double a missed dose.",
                 "Main dose ya bimari ke baare mein salah nahi de sakta. Kripya doctor ya pharmacist se poochhiye. Chhooti dose ko kabhi dugna na lein.")
    elif i == "taken":
        d = next((x for x in doses if x["status"] in ("unconfirmed", "upcoming")), None)
        if d:
            r = pick(f"Shall I mark {d['name']} as taken? Tap Yes to confirm.", f"Kya main {d['name']} ko le li mark kar doon? Confirm karne ke liye Yes dabayein.")
            action = {"type": "confirm_dose", "dose_id": d["id"]}
        else: r = pick("All of today's doses are already recorded.", "Aaj ki sabhi dawaiyan pehle se darj hain.")
    elif i == "medicines":
        r = "; ".join(f"{x['name']} {x['dosage']} at {x['due'][11:]} ({x['status']})" for x in doses)
        r = pick("Today: ", "Aaj: ") + r if r else pick("No medicines are scheduled today.", "Aaj koi dawa nahi hai.")
    elif i == "appointment":
        r = pick(f"Your next appointment is with Dr {appt['doctor']} on {appt['when_at'].replace('T', ' ')}.", f"Aapka agla appointment Dr {appt['doctor']} ke saath {appt['when_at'].replace('T', ' ')} ko hai.") if appt else pick("You have no upcoming appointments.", "Koi agla appointment nahi hai.")
    elif i == "call":
        r = pick("Calling is not set up yet. Use the red Emergency button to alert your family.", "Call ki suvidha abhi shuru nahi hui hai. Parivar ko alert karne ke liye laal Emergency button dabayein.")
    elif i == "summary": r = summary()
    else:
        r = ask_llm(text, summary()) or pick("I can tell you today's medicines, your next appointment, or mark a dose as taken. What would you like?", "Main aaj ki dawaiyan, agla appointment bata sakta hoon, ya dose le li mark kar sakta hoon. Aap kya chahte hain?")
    return {"reply": r, "lang": L, "intent": i, "action": action}
