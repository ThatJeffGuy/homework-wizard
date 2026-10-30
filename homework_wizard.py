#!/usr/bin/env python3
"""Homework Wizard: a student's own draft in, a fact check and coaching report out. Fully local.

It checks work, it does not write it: every suggestion is a pointer (what to look into, which source says so),
never text that could be pasted in.

Teacher mode also drafts sourced additions and new sections. Only teachers who log in get it: the school adds
teachers (email + password) with  python3 add_teacher.py  (see README).

Made by That Scottish Guy (https://www.everydaysciencestuff.com) - MIT licence, see LICENSE.

Pipeline per job (each step is saved to state.json, so a restart resumes where it stopped):
  sections -> claims -> research (SearXNG + page fetch) -> verdicts -> topic ideas -> tips -> done
The student's text is never rewritten.
"""
import hashlib, hmac, html, json, os, re, secrets, sys, threading, time, traceback, urllib.parse, urllib.request
from http.cookies import SimpleCookie
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:11436/api/chat")
MODEL = os.environ.get("LLM_MODEL", "glm-4.7-flash:q4_K_M")
SEARX = os.environ.get("SEARX_URL", "http://127.0.0.1:8080/search")
DATA = os.environ.get("DATA_DIR", "/var/lib/homework-wizard/jobs")
PORT = int(os.environ.get("PORT", "8501"))
THREADS = int(os.environ.get("THREADS", "0"))  # CPU threads for the model; 0 = let Ollama decide
WPM = 238
UA = "Mozilla/5.0 (X11; Linux x86_64) HomeworkWizard/1.0"
REPO = "https://github.com/ThatJeffGuy/homework-wizard"
STEPS = ["sections", "claims", "research", "verdicts", "newsections", "additions", "done"]

wake = threading.Event()
_base = os.path.dirname(os.path.abspath(DATA))
KEYS = os.environ.get("TEACHERS_FILE", os.path.join(_base, "teachers.json"))  # teacher logins (hashed passwords)
SECRET_FILE = os.environ.get("SECRET_FILE", os.path.join(_base, "secret.key"))  # signs teacher sessions
SESSION_DAYS = 30
STUDENTS_FILE = os.path.join(_base, "students.json")   # hint balance per browser
SETTINGS_FILE = os.path.join(_base, "settings.json")   # teacher-adjustable settings


# ---------- storage ----------
def jpath(jid, name="state.json"):
    return os.path.join(DATA, jid, name)

def load(jid):
    with open(jpath(jid)) as f:
        return json.load(f)

def save(st):
    p = jpath(st["id"])
    with open(p + ".tmp", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(p + ".tmp", p)

def log(st, msg):
    line = time.strftime("%H:%M:%S ") + msg
    st.setdefault("log", []).append(line)
    st["log"] = st["log"][-200:]
    print(st["id"], line, flush=True)
    save(st)

def all_jobs():
    out = []
    for jid in os.listdir(DATA):
        try:
            out.append(load(jid))
        except Exception:
            pass
    return sorted(out, key=lambda s: s["created"], reverse=True)


# ---------- helpers ----------
def words(text):
    return len(re.findall(r"\b\w[\w'’-]*\b", text))

def read_min(n):
    return max(1, round(n / WPM))

def split_sections(text):
    """Headings = short lines without sentence-ending punctuation (or ending in ':')."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\S)", text.strip()) if p.strip()]
    secs = [{"title": "Intro", "paras": []}]
    for p in paras:
        is_head = len(p) < 80 and (p.endswith(":") or not re.search(r"[.!?…\"”)]$", p))
        if is_head:
            secs.append({"title": p.rstrip(":").strip("# ").strip(), "paras": []})
        else:
            secs[-1]["paras"].append(p)
    return [s for s in secs if s["paras"]]

def llm(messages, as_json=False, temp=0.3, max_tokens=1200):
    body = {"model": MODEL, "messages": messages, "stream": False, "think": False,
            "options": {"num_ctx": 16384, "temperature": temp, "num_predict": max_tokens}}
    if THREADS:
        body["options"]["num_thread"] = THREADS
    if as_json:
        body["format"] = "json"
    req = urllib.request.Request(LLM_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        out = json.load(r)["message"]["content"].strip()
    if not as_json:
        return out
    try:
        return json.loads(out)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", out, re.S)
        return json.loads(m.group(0)) if m else {}


class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript", "form", "svg"}
    def __init__(self):
        super().__init__(); self.parts = []; self.skip = 0
    def handle_starttag(self, tag, a):
        if tag in self.SKIP: self.skip += 1
        if tag in ("p", "br", "li", "h1", "h2", "h3", "div", "tr"): self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip: self.skip -= 1
    def handle_data(self, d):
        if not self.skip: self.parts.append(d)

def fetch_text(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            if "html" not in r.headers.get("Content-Type", "html"):
                return ""
            raw = r.read(600_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
        p = _Text(); p.feed(raw)
        txt = re.sub(r"[ \t]+", " ", "".join(p.parts))
        return "\n".join(l.strip() for l in txt.splitlines() if len(l.strip()) > 40)
    except Exception:
        return ""

def search(q, n=6):
    url = SEARX + "?" + urllib.parse.urlencode({"q": q, "format": "json", "language": "en"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
            res = json.load(r).get("results", [])
    except Exception:
        return []
    seen, out = set(), []
    for x in res:
        u = x.get("url", "")
        dom = urllib.parse.urlparse(u).netloc
        if not u or dom in seen or re.search(r"youtube\.|tiktok\.|facebook\.|instagram\.|pinterest\.", dom):
            continue
        seen.add(dom); out.append({"url": u, "title": x.get("title", ""), "snippet": x.get("content", "")})
        if len(out) >= n:
            break
    return out

def best_chunks(text, query, k=3, size=700):
    terms = {w for w in re.findall(r"[a-z0-9.]{3,}", query.lower())}
    chunks, buf = [], ""
    for line in text.splitlines():
        buf += line + " "
        if len(buf) > size:
            chunks.append(buf.strip()); buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    scored = sorted(chunks, key=lambda c: -sum(c.lower().count(t) for t in terms))
    return [c for c in scored[:k] if any(t in c.lower() for t in terms)]

def research(query, pages=3):
    """Returns evidence items [{url,title,text}] for one query."""
    ev = []
    for i, r in enumerate(search(query)):
        txt = ""
        if i < pages:
            body = fetch_text(r["url"])
            txt = " … ".join(best_chunks(body, query + " " + r["title"])) if body else ""
        ev.append({"url": r["url"], "title": r["title"], "text": (txt or r["snippet"])[:2200]})
    return [e for e in ev if e["text"]][:5]

def shingles(text, n=8):
    w = re.findall(r"[a-z0-9]+", text.lower().replace("’", "'"))
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}

def not_copies(ev, draft):
    """Drop sources that repeat the draft itself (e.g. an already published copy of it): checking a text against
    itself proves nothing. A source sharing 3+ runs of 8 words with the draft counts as a copy."""
    mine = shingles(draft)
    return [x for x in ev if len(shingles(x["text"]) & mine) < 3]

def evidence_block(ev):
    return "\n\n".join(f"[{i+1}] {e['title']} ({e['url']})\n{e['text']}" for i, e in enumerate(ev))


SYS = ("You are a careful fact-checker and research assistant. You never invent facts: "
       "anything you state must come from the numbered evidence you are given, and you cite it like [2]. "
       "If the evidence does not support something, say so plainly.")
SYS_STUDENT = ("You are a patient teacher checking a student's own homework. You point out what is accurate, what "
               "is wrong and what could be stronger, and you point them to the sources to read, cited like [2]. You "
               "never write their work for them and never invent facts: anything you state must come from the numbered "
               "evidence you are given. If the evidence does not support something, say so plainly.")


# ---------- pipeline steps ----------
def step_sections(st):
    st["sections"] = split_sections(st["text"])
    log(st, f"split into {len(st['sections'])} sections: " + ", ".join(s["title"] for s in st["sections"]))

def step_claims(st):
    for si, s in enumerate(st["sections"]):
        if "claims" in s:
            continue
        r = llm([{"role": "system", "content": SYS}, {"role": "user", "content":
            "List the checkable factual claims in this section of the article: real-world numbers, science, "
            "dates, names, events, and statements about what happens in books, films, shows or history. Skip pure opinion "
            "and jokes. Max 6, most important first. For each give a short web search query that would verify it.\n"
            'Reply as JSON: {"claims":[{"claim":"...","query":"..."}]}\n\nSECTION "' + s["title"] + '":\n'
            + "\n\n".join(s["paras"])}], as_json=True)
        s["claims"] = [c for c in (r.get("claims") or []) if c.get("claim") and c.get("query")][:6]
        log(st, f"claims for '{s['title']}': {len(s['claims'])}")

def step_research(st):
    todo = [c for s in st["sections"] for c in s["claims"] if "evidence" not in c]
    for n, c in enumerate(todo, 1):
        c["evidence"] = not_copies(research(c["query"]), st["text"])
        log(st, f"researched {n}/{len(todo)}: {c['query']} ({len(c['evidence'])} sources)")

def step_verdicts(st):
    todo = [c for s in st["sections"] for c in s["claims"] if "verdict" not in c]
    for n, c in enumerate(todo, 1):
        if not c["evidence"]:
            c.update(verdict="unclear", note="No usable sources found.", correction="", cites=[])
        else:
            r = llm([{"role": "system", "content": SYS}, {"role": "user", "content":
                "Check this claim from the article against the evidence only.\nCLAIM: " + c["claim"] +
                "\n\nEVIDENCE:\n" + evidence_block(c["evidence"]) +
                '\n\nReply as JSON: {"verdict":"supported|partly|unclear|contradicted","note":"one or two sentences '
                'explaining why, with [n] citations","correction":"the accurate version if partly/contradicted, else empty",'
                '"cites":[numbers of the sources you used]}'}], as_json=True, temp=0.1, max_tokens=500)
            v = str(r.get("verdict", "unclear")).lower()
            c.update(verdict=v if v in ("supported", "partly", "unclear", "contradicted") else "unclear",
                     note=r.get("note", ""), correction=r.get("correction", ""),
                     cites=[i for i in r.get("cites", []) if isinstance(i, int) and 0 < i <= len(c["evidence"])])
        log(st, f"verdict {n}/{len(todo)}: {c['verdict']} - {c['claim'][:70]}")

def norm(t):
    t = t.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9]+", " ", t).strip()

def step_newsections(st):
    if "new" not in st:
        heads = [s["title"] for s in st["sections"][1:]]
        r = llm([{"role": "system", "content": SYS}, {"role": "user", "content":
            "Read the article below. First name its SUBJECT: the specific person, character, thing or event it is about.\n"
            f"It already has sections on: {', '.join(heads)}. Find up to {st['extra']} things the article itself MENTIONS "
            "about the subject but never gives its own section. Only use what is written in the article: do not add "
            "traits, powers, events or facts from your own knowledge. For each give: a heading; 'quote' = the exact words "
            "from the article that mention it (copied verbatim, 5-25 words); a one-line angle saying which real-world "
            "science or facts would test it; and 3 web search queries: the first about how the subject is shown doing "
            "or having this (include the subject's name), the other two about the real-world science or numbers.\n"
            'Reply as JSON: {"subject":"...","sections":[{"title":"...","quote":"...","angle":"...","queries":["...","...","..."]}]}'
            "\n\nARTICLE:\n" + st["text"]}], as_json=True, temp=0.2)
        st["subject"] = str(r.get("subject") or "").strip()
        body, keep = norm(st["text"]), []
        for x in r.get("sections") or []:
            q = norm(str(x.get("quote", "")))
            if x.get("title") and x.get("queries") and len(q) > 10 and q in body:
                keep.append(x)
            else:
                log(st, f"dropped idea '{x.get('title')}': its quote is not in the draft")
        st["new"] = keep[:st["extra"]]
        log(st, f"subject: {st['subject']}; new sections planned: " + (", ".join(x["title"] for x in st["new"]) or "none"))
    subject = st.get("subject") or "the article's subject"
    sample = "\n\n".join(st["sections"][1]["paras"]) if len(st["sections"]) > 1 else st["text"][:1500]
    intro = "\n\n".join(st["sections"][0]["paras"])[:3000]
    for x in st["new"]:
        if "evidence" not in x:
            ev = []
            for q in x["queries"][:3]:
                ev += research(q, pages=2)
            x["evidence"] = not_copies(ev, st["text"])[:8]
            log(st, f"researched new section '{x['title']}' ({len(x['evidence'])} sources)")
        anchor = (f"SUBJECT: {subject}\nWHAT THE DRAFT SAYS: \"{x.get('quote', '')}\"\nANGLE: {x['angle']}\n"
                  f"Stay about {subject} and only what the draft says about it; do not add traits, powers or events "
                  "that are not in the draft.")
        if is_student(st) and "tips" not in x:
            x["tips"] = llm([{"role": "system", "content": SYS_STUDENT}, {"role": "user", "content":
                f"The student could add a section about \"{x['title']}\".\n{anchor}\nGive 2 to 4 short bullet points "
                "(start each with '- '): what that section could cover, which questions it should answer, and which source "
                "to read for each, cited like [2]. Coach, don't write: no sentences they could copy into their work.\n\n"
                "EVIDENCE:\n" + evidence_block(x["evidence"])}], temp=0.4, max_tokens=500)
            log(st, f"tips for possible section '{x['title']}'")
        if not is_student(st):
            teacher_section(st, x, subject, anchor, intro, sample)

def step_additions(st):
    for s in st["sections"]:
        if "additions" in s:
            continue
        ev, notes = [], []
        for c in s["claims"]:
            ev += c["evidence"]
            notes.append(f"- {c['claim']} => {c['verdict']}. {c.get('correction') or ''}")
        ev = ev[:10]
        if not ev:
            s["additions"] = []; continue
        numbered = "\n\n".join(f"P{i+1}: {p}" for i, p in enumerate(s["paras"]))
        if is_student(st):
            r = llm([{"role": "system", "content": SYS_STUDENT}, {"role": "user", "content":
                "Give 1 to 3 pieces of feedback that would make this section stronger: a point that needs evidence, an "
                "idea worth explaining further, a missing comparison, or a claim to fix. Each one 15-45 words, addressed "
                "to the student as a tip or question, pointing to the source that helps with [n]. Never write sentences "
                "they could paste into their work. Say which paragraph it is about.\n"
                'Reply as JSON: {"additions":[{"after":<paragraph number>,"why":"short label","text":"..."}]}\n\n'
                f"SUBJECT: {st.get('subject') or '(see the section)'}. Stay about it; add nothing the draft and evidence do not support.\n\n"
                "FACT-CHECK RESULTS:\n" + "\n".join(notes) + f"\n\nSECTION \"{s['title']}\":\n" + numbered +
                "\n\nEVIDENCE:\n" + evidence_block(ev)}], as_json=True, temp=0.4, max_tokens=700)
            s["add_evidence"] = ev
            s["additions"] = [a for a in (r.get("additions") or []) if a.get("text")][:3]
            log(st, f"feedback for '{s['title']}': {len(s['additions'])}")
            continue
        teacher_additions(st, s, ev, notes, numbered)


def is_student(st):
    return st.get("mode", "student") == "student"

def run_job(jid):
    st = load(jid)
    st.setdefault("started", time.time())
    try:
        while st["step"] != "done":
            st["status"] = "running"; log(st, "step: " + st["step"])
            {"sections": step_sections, "claims": step_claims, "research": step_research, "verdicts": step_verdicts,
             "newsections": step_newsections, "additions": step_additions}[st["step"]](st)
            st["step"] = STEPS[STEPS.index(st["step"]) + 1]; save(st)
        award_hint(st)
        st["status"] = "done"; st["finished"] = time.time()
        log(st, f"finished in {round((st['finished'] - st['created']) / 60)} min")
    except Exception as e:
        st["status"] = "error"; st["error"] = repr(e)
        log(st, "ERROR " + "".join(traceback.format_exception_only(e)).strip())
        print(traceback.format_exc(), flush=True)

def worker():
    while True:
        pending = [s for s in all_jobs() if s["status"] in ("queued", "running")]
        if pending:
            run_job(sorted(pending, key=lambda s: s["created"])[0]["id"])
            continue
        wake.wait(30); wake.clear()


# ---------- teacher mode (drafts sourced text; only for signed-in teachers) ----------
SYS_TEACHER = ("You are a careful research editor. You help the writer flesh out and fact-check their own draft, matching "
               "their tone, voice and spelling conventions. You never invent facts: anything you state must come from the "
               "numbered evidence you are given, and you cite it like [2]. If the evidence does not support something, "
               "say so plainly.")

def teacher_section(st, x, subject, anchor, intro, sample):
    if "draft" in x:
        return
    x["draft"] = llm([{"role": "system", "content": SYS_TEACHER}, {"role": "user", "content":
        f"Write a new section titled \"{x['title']}\" for the article.\n{anchor}\n"
        f"Structure: start from what {subject} is shown doing (the draft's words above), then test it against "
        "real-world science and numbers from the evidence, then conclude. Every paragraph must tie back to "
        f"{subject}. 200-350 words in the writer's voice (see the samples). Use ONLY facts from the evidence, "
        "cited like [2]; never write 'the article' or 'the source' - name the source or just cite it. Where the "
        "evidence is thin, say what is unknown instead of guessing. If the writer's sections end with a "
        "recurring line or verdict, end the same way.\n\n"
        "ARTICLE INTRO:\n" + intro + "\n\nWRITER SAMPLE SECTION:\n" + sample +
        "\n\nEVIDENCE:\n" + evidence_block(x["evidence"]) + "\n\nWrite only the section body, no heading."}],
        temp=0.6, max_tokens=900)
    log(st, f"drafted new section '{x['title']}' ({words(x['draft'])} words)")

def teacher_additions(st, s, ev, notes, numbered):
    r = llm([{"role": "system", "content": SYS_TEACHER}, {"role": "user", "content":
        "Suggest 1 to 3 additions that flesh out this section for a longer read: extra real-world detail, a "
        "comparison, a number with context, or a short explanation of the science. Do NOT repeat or rewrite the "
        "writer's text. Each addition 50-150 words, in the writer's voice, ONLY facts from the evidence, with [n] "
        "citations. Say after which paragraph it goes.\n"
        'Reply as JSON: {"additions":[{"after":<paragraph number>,"why":"short reason","text":"..."}]}\n\n'
        f"SUBJECT: {st.get('subject') or '(see the section)'}. Stay about it; add nothing the draft and evidence do not support.\n\n"
        "FACT-CHECK RESULTS:\n" + "\n".join(notes) + f"\n\nSECTION \"{s['title']}\":\n" + numbered +
        "\n\nEVIDENCE:\n" + evidence_block(ev)}], as_json=True, temp=0.6, max_tokens=1200)
    s["add_evidence"] = ev
    s["additions"] = [a for a in (r.get("additions") or []) if a.get("text")][:3]
    log(st, f"additions for '{s['title']}': {len(s['additions'])}")


# ---------- teacher logins + sessions ----------
PBKDF2_ROUNDS = 600_000
MIN_PASSWORD = 10
LOCK_ATTEMPTS, LOCK_SECONDS = 3, 3600   # 3 wrong passwords = that address is locked out for 1 hour
IP_HEADER = os.environ.get("CLIENT_IP_HEADER", "")  # e.g. CF-Connecting-IP, only if your proxy always sets it
failed, locked = {}, {}   # ip -> [failure times] / ip -> locked until

def read_keys():
    try:
        with open(KEYS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def write_keys(keys):
    os.makedirs(os.path.dirname(KEYS) or ".", exist_ok=True)
    with open(KEYS + ".tmp", "w") as f:
        json.dump(keys, f, indent=1)
    os.chmod(KEYS + ".tmp", 0o600)
    os.replace(KEYS + ".tmp", KEYS)

def norm_email(email):
    return email.strip().lower()

def hash_password(password, salt, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds).hex()

def set_teacher(email, password):
    keys = read_keys()
    salt = secrets.token_hex(16)
    keys[norm_email(email)] = {"salt": salt, "rounds": PBKDF2_ROUNDS, "hash": hash_password(password, salt),
                               "created": time.strftime("%Y-%m-%d")}
    write_keys(keys)

def check_login(email, password):
    """Email of the teacher if the password matches, else None (same work either way, so timing reveals nothing)."""
    rec = read_keys().get(norm_email(email))
    salt = rec.get("salt", "00" * 16) if rec else "00" * 16
    rounds = rec.get("rounds", PBKDF2_ROUNDS) if rec else PBKDF2_ROUNDS
    ok = hmac.compare_digest(hash_password(password, salt, rounds), rec.get("hash", "") if rec else "")
    return norm_email(email) if (rec and ok) else None

def lock_left(ip):
    """Seconds this address is still locked out for (0 = may try)."""
    return max(0, int(locked.get(ip, 0) - time.time()))

def login_failed(ip):
    now = time.time()
    failed[ip] = [t for t in failed.get(ip, []) if now - t < LOCK_SECONDS] + [now]
    if len(failed[ip]) >= LOCK_ATTEMPTS:
        locked[ip] = now + LOCK_SECONDS
        failed.pop(ip)

def server_secret():
    os.makedirs(os.path.dirname(SECRET_FILE) or ".", exist_ok=True)
    if not os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(SECRET_FILE, 0o600)
    with open(SECRET_FILE) as f:
        return f.read().strip().encode()

def make_session(name):
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    body = urllib.parse.quote(name) + "|" + exp
    return body + "|" + hmac.new(server_secret(), body.encode(), "sha256").hexdigest()

def session_teacher(cookie):
    """Name of the signed-in teacher, or None. Removing a teacher's key also ends their sessions."""
    try:
        qname, exp, sig = cookie.split("|")
    except (AttributeError, ValueError):
        return None
    body = qname + "|" + exp
    if not hmac.compare_digest(sig, hmac.new(server_secret(), body.encode(), "sha256").hexdigest()):
        return None
    name = urllib.parse.unquote(qname)
    return name if int(exp) > time.time() and name in read_keys() else None


# ---------- hints: earned by good scores, spent on a highlighted part of a marked paper ----------
MAX_HINTS = 3
file_lock = threading.Lock()

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)

def settings():
    cfg = {"hint_score": 80, "start_hints": 1}
    cfg.update(read_json(SETTINGS_FILE, {}))
    return cfg

def hints_left(viewer):
    rec = read_json(STUDENTS_FILE, {}).get(viewer or "", {})
    return rec.get("hints", min(MAX_HINTS, settings()["start_hints"]))

def change_hints(viewer, delta):
    """Add (or spend, with a negative delta) hints; returns the new balance, or None if there is nothing to spend."""
    with file_lock:
        students = read_json(STUDENTS_FILE, {})
        rec = students.setdefault(viewer, {})
        have = rec.get("hints", min(MAX_HINTS, settings()["start_hints"]))
        if have + delta < 0:
            return None
        rec["hints"] = min(MAX_HINTS, have + delta)
        write_json(STUDENTS_FILE, students)
        return rec["hints"]

def score(st):
    claims = [c for s in st.get("sections", []) for c in s.get("claims", []) if "verdict" in c]
    return round(100 * sum(c["verdict"] == "supported" for c in claims) / len(claims)) if claims else None

def award_hint(st):
    """A marked student paper scoring at least the teacher's threshold earns one hint (once per paper)."""
    pct, need = score(st), settings()["hint_score"]
    if not is_student(st) or not st.get("owner") or pct is None or st.get("hint_award"):
        return
    if pct >= need:
        before = hints_left(st["owner"])
        after = change_hints(st["owner"], +1)
        st["hint_award"] = {"score": pct, "earned": after > before}
    else:
        st["hint_award"] = {"score": pct, "earned": False, "needed": need}

def make_hint(jid, hid):
    with file_lock:
        st = load(jid)
    h = st["hints"][hid]
    si = int(h["k"][1:].split("_")[0]); pi = int(h["k"].split("_")[1])
    sec = st["sections"][si]
    ev = sec.get("add_evidence") or [x for c in sec.get("claims", []) for x in c.get("evidence", [])][:8]
    notes = "\n".join(f"- {c['claim']} => {c.get('verdict')}: {c.get('note', '')}" for c in sec.get("claims", []))
    try:
        text = llm([{"role": "system", "content": SYS_STUDENT}, {"role": "user", "content":
            "The student highlighted part of their own homework and asked for a hint.\n"
            f"HIGHLIGHTED: \"{h['text']}\"\nIN THIS PARAGRAPH: {sec['paras'][pi - 1]}\n\n"
            "Give ONE hint of 1 to 3 sentences, written to the student. Point them in the right direction: a guiding "
            "question, what to double-check, or what to look for, and which source to read, cited like [2]. Do NOT give "
            "the answer, do NOT correct it for them and do NOT rewrite their words. If the highlighted part already "
            "looks right, say so and hint at how to back it up or explain it better.\n\n"
            "MARKING SHEET FOR THIS SECTION:\n" + (notes or "(none)") + "\n\nEVIDENCE:\n" + evidence_block(ev)}],
            temp=0.4, max_tokens=300)
        status = "done"
    except Exception as ex:
        text, status = f"The hint could not be made ({ex.__class__.__name__}); any hint spent on it was given back.", "error"
        if h.get("paid_by"):
            change_hints(h["paid_by"], +1)
    with file_lock:
        st = load(jid)
        st["hints"][hid].update(answer=text, status=status, evidence=ev)
        save(st)

# ---------- web ----------
CSS = """
:root{--paper:#fdfcf6;--ink:#1f2a44;--mut:#5b6377;--rule:#d6e4f2;--margin:#f0a8a8;--card:#fff;--line:#d9dce3;
--acc:#1d58b0;--pen:#c62828;--ok:#2e7d32;--half:#b86e00;--q:#5b6377;--bad:#c62828;
--sticky:#fff4a3;--sticky-ink:#3b3410;--sticky2:#d9f5c9;--pencil:#ffcd3c;--pencil-edge:#c99700;--hl:#fff27a;
--hand:"Segoe Print","Bradley Hand","Chalkboard SE","Marker Felt","Comic Neue","Patrick Hand","Comic Sans MS",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--paper:#21342b;--ink:#eef2ea;--mut:#b5c2b8;--rule:rgba(255,255,255,.05);
--margin:rgba(255,140,140,.35);--card:#2b4136;--line:#3f5a4c;--acc:#9fd0ff;--pen:#ff8f85;--ok:#a5e8a1;--half:#ffcf86;
--q:#cfd8d2;--bad:#ff8f85;--sticky:#e9dc78;--sticky2:#bfe3ad;--hl:rgba(255,242,122,.25)}}
*{box-sizing:border-box}
body{margin:0;color:var(--ink);font:16px/1.65 system-ui,sans-serif;background:var(--paper)
 repeating-linear-gradient(transparent 0 31px,var(--rule) 31px 32px)}
.wrap{position:relative;max-width:880px;min-height:100vh;margin:0 auto;padding:28px 18px 80px 46px}
h1 svg{width:1.15em;height:1.15em;vertical-align:-.12em;margin-right:.15em}
.wrap::before{content:"";position:absolute;top:0;bottom:0;left:30px;border-left:2px solid var(--margin)}
h1,h2,h3,summary b,.hand{font-family:var(--hand)}
h1{font-size:2.1rem;margin:.1em 0;line-height:1.2}
h1 .hl{background:linear-gradient(transparent 58%,var(--hl) 58% 92%,transparent 92%)}
h2{font-size:1.4rem;margin:2em 0 .4em}
.motto{font-family:var(--hand);font-size:1.15rem;color:var(--pen);transform:rotate(-1deg);display:inline-block;margin:.2em 0 .6em}
.mut{color:var(--mut);font-size:.92rem}a{color:var(--acc)}
textarea,input,select{width:100%;font:inherit;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
textarea{min-height:340px;line-height:26px;padding:12px 12px;background:var(--card)
 repeating-linear-gradient(transparent 0 25px,var(--rule) 25px 26px);background-position:0 12px;background-attachment:local}
label.num{display:flex;align-items:center;gap:8px;font-size:.92rem;color:var(--mut)}label.num input{width:5em}
button{font:700 1rem system-ui,sans-serif;padding:9px 20px;border:0;border-radius:4px 16px 16px 4px;background:var(--pencil);
 color:#2b2100;box-shadow:0 3px 0 var(--pencil-edge);cursor:pointer}
button:active{transform:translateY(2px);box-shadow:0 1px 0 var(--pencil-edge)}
button.link{background:none;box-shadow:none;color:var(--acc);padding:0;text-decoration:underline;font-weight:400;border-radius:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px 16px;margin:12px 0;box-shadow:1px 2px 0 rgba(0,0,0,.06)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.row>*{flex:1 1 170px}
.v{display:inline-block;font:700 .9rem var(--hand);padding:0 .45em;border:2px solid currentColor;border-radius:7px;
 margin:0 .5em .2em 0;transform:rotate(-3deg);white-space:nowrap}
.supported{color:var(--ok)}.partly{color:var(--half)}.unclear,.pending{color:var(--q)}.contradicted{color:var(--bad)}
.claims{font-size:.93rem}.claims ul{padding-left:1.1em}.claims li{margin:.7em 0;list-style:none}
.add{background:var(--sticky);color:var(--sticky-ink);border:0;border-radius:2px;transform:rotate(-.5deg);
 box-shadow:2px 4px 8px rgba(0,0,0,.15);margin:14px 6px}
.add:nth-of-type(even){transform:rotate(.6deg)}.add.pickable{background:var(--sticky2)}.add a{color:#1a4f9c}
.add .mut{color:#6b6230}.add b.hand{font-size:1.05rem}
.newsec{border-radius:3px;padding-top:8px}
.newsec>b:first-child,.newsec>label{display:block;border-bottom:2px solid var(--margin);padding-bottom:.35em;margin-bottom:.5em;font-family:var(--hand)}
.add label,.newsec label{font-weight:700;display:flex;gap:8px;align-items:center;cursor:pointer}.add label input,.newsec label input{width:auto}
.src{font-size:.82rem}.src a{word-break:break-all}.orig p{margin:.7em 0}
.score{display:inline-block;font:700 1.35rem var(--hand);color:var(--pen);border:3px solid var(--pen);border-radius:50%;
 padding:.35em 1em;transform:rotate(-4deg);margin:.4em 0 .2em}
pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,monospace;background:var(--card);border:1px dashed var(--line);border-radius:4px;padding:10px;max-height:260px;overflow:auto}
footer{margin-top:48px;padding-top:12px;border-top:1px solid var(--line);color:var(--mut);font-size:.85rem;text-align:center}
.stats{display:flex;gap:6px 22px;flex-wrap:wrap;font-weight:600;align-items:center}
table{width:100%;border-collapse:collapse}td{padding:7px 4px;border-bottom:1px solid var(--rule)}
details.teacher{margin-top:32px}details.teacher form{display:flex;gap:10px;flex-wrap:wrap}details.teacher input{flex:1 1 220px;width:auto}
summary{cursor:pointer}
.progress{position:sticky;top:0;z-index:5;background:var(--paper);margin:-28px -18px 10px -16px;padding:12px 18px 10px 16px;
 border-bottom:1px solid var(--line)}
.bar{height:16px;border:2px solid var(--ink);border-radius:3px 10px 10px 3px;background:var(--card);overflow:hidden}
.bar i{display:block;height:100%;background:repeating-linear-gradient(45deg,var(--pencil) 0 10px,#ffe07a 10px 20px);
 border-right:3px solid var(--pencil-edge);transition:width .6s;animation:slide 1.2s linear infinite;background-size:28px 28px}
@keyframes slide{to{background-position:28px 0}}
@media (prefers-reduced-motion:reduce){.bar i{animation:none}}
.plabel{font-size:.9rem;margin-top:4px;color:var(--mut)}.plabel b{color:var(--ink)}
@media (max-width:520px){.wrap{padding-left:30px}.wrap::before{left:16px}h1{font-size:1.7rem}}
"""

FOOTER = (f"<footer><b>Made by <a href='https://www.everydaysciencestuff.com' target=_blank rel=noopener>That Scottish Guy</a></b>"
          f" · &copy; 2026 · <a href='{REPO}/blob/main/LICENSE' target=_blank rel=noopener>MIT</a>"
          f" · <a href='{REPO}' target=_blank rel=noopener>Source on GitHub</a></footer>")

def page(title, body, refresh=False, side=""):
    # reload every 15 s while work is in progress, but never while a drawer is open or text is highlighted
    again = ("<script>setInterval(()=>{if(!document.querySelector('.side.open')&&!document.hidden&&!getSelection().toString())"
             "location.reload()},15000)</script>") if refresh else ""
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}{DRAWER_CSS}</style></head><body><div class=wrap>{body}{FOOTER}</div>"
            f"{side}{again}</body></html>")

def e(s):
    return html.escape(str(s or ""))

def cite_links(text, ev):
    t = e(text)
    def sub(m):
        out = []
        for n in re.findall(r"\d+", m.group(0)):
            i = int(n) - 1
            out.append(f'<a href="{e(ev[i]["url"])}" target=_blank title="{e(ev[i]["title"])}">{n}</a>' if 0 <= i < len(ev) else n)
        return "[" + ",".join(out) + "]"
    return re.sub(r"\[\d+(?:\s*,\s*\d+)*\]", sub, t)

HAT = ("<svg viewBox='0 0 64 64' aria-hidden=true><path d='M6 52Q32 43 58 52Q32 61 6 52Z' fill='currentColor'/>"
       "<path d='M14 50L33 7Q37 3 38 9L47 49Q30 44 14 50Z' fill='currentColor'/>"
       "<path d='M27 27l2.2 4.4 4.8.7-3.5 3.4.8 4.8-4.3-2.3-4.3 2.3.8-4.8-3.5-3.4 4.8-.7z' fill='#ffcd3c'/>"
       "<circle cx='38' cy='19' r='2.2' fill='#ffcd3c'/><circle cx='22' cy='44' r='1.6' fill='#ffcd3c'/></svg>")


# ---------- side drawers: student tools (left) and staff room (right) ----------
DRAWER_CSS = """
:root{--side-w:min(390px,92vw)}
.side{position:fixed;top:0;bottom:0;width:var(--side-w);z-index:45;overflow-y:auto;padding:22px 20px 48px;
 background:var(--card);box-shadow:0 0 34px rgba(0,0,0,.28);visibility:hidden;
 transition:transform .45s cubic-bezier(.2,.8,.2,1),visibility 0s .45s}
.side.left{left:0;transform:translateX(-102%);border-right:3px solid var(--pencil-edge)}
.side.right{right:0;transform:translateX(102%);border-left:3px solid var(--pen)}
.side.open{visibility:visible;transform:none;transition:transform .45s cubic-bezier(.2,.8,.2,1)}
.side h2{margin:.2em 0 .3em;padding:0;border:0}.side h3{font-size:1.05rem;margin:0}
button.side-tab{position:fixed;top:50%;z-index:46;display:flex;flex-direction:column;align-items:center;gap:12px;
 padding:18px 9px;border:0;font:700 11px/1 system-ui,sans-serif;letter-spacing:.24em;text-transform:uppercase;opacity:.9;
 box-shadow:0 4px 14px rgba(0,0,0,.22);transition:left .45s cubic-bezier(.2,.8,.2,1),right .45s cubic-bezier(.2,.8,.2,1),opacity .2s,padding .2s}
button.side-tab:hover{opacity:1}button.side-tab:active{transform:translateY(-50%)}
.side-tab .tab-txt{writing-mode:vertical-rl}
.side-tab .tab-arrow{width:9px;height:9px;border:0 solid currentColor;border-width:0 0 2.5px 2.5px;flex:none;transition:transform .4s}
button.side-tab.right{right:0;transform:translateY(-50%);border-radius:12px 0 0 12px;background:var(--pen);color:#fff}
button.side-tab.left{left:0;transform:translateY(-50%);border-radius:0 12px 12px 0;background:var(--pencil);color:#2b2100}
.side-tab.left .tab-txt{transform:rotate(180deg)}
.side-tab.right .tab-arrow{transform:rotate(45deg)}.side-tab.left .tab-arrow{transform:rotate(-135deg)}
.side.open+.side-tab.right{right:var(--side-w)}.side.open+.side-tab.left{left:var(--side-w)}
.side.open+.side-tab.right .tab-arrow{transform:rotate(-135deg)}.side.open+.side-tab.left .tab-arrow{transform:rotate(45deg)}
.side-tab.right:hover{padding-right:14px}.side-tab.left:hover{padding-left:14px}
.tool{border:1px solid var(--line);border-radius:6px;margin:10px 0;background:var(--paper)}
.tool>summary{padding:9px 12px;font-weight:700}.tool>div{padding:0 12px 12px}
.tool label{display:block;font-size:.85rem;color:var(--mut);margin-top:6px}.tool input,.tool select,.tool textarea{padding:7px 9px}
.tool textarea{min-height:110px}.tool button{padding:6px 14px;margin-top:8px}
.tool .out{background:var(--card);border:1px dashed var(--line);border-radius:4px;padding:8px 10px;margin-top:8px;font-size:.92rem;word-break:break-word}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:.92rem}.kv b{font-weight:600}
.checks label{display:flex;gap:8px;align-items:flex-start;color:var(--ink);font-size:.93rem}.checks input{width:auto;margin-top:5px}
.timer{font:700 2.4rem/1.1 ui-monospace,monospace;text-align:center;margin:6px 0}
.cv{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.star{color:var(--pencil-edge);font-size:1.2rem;letter-spacing:.1em}.star.off{color:var(--line)}
.hintbox{background:var(--paper)}.hintbox p{margin:.3em 0}
#hintbar{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:40;display:none;max-width:92vw;
 background:var(--card);border:2px solid var(--ink);border-radius:10px;padding:8px 10px 8px 14px;box-shadow:0 6px 20px rgba(0,0,0,.25);
 align-items:center;gap:12px}#hintbar.show{display:flex}#hintbar.nohints{border-style:dashed;color:var(--mut)}
button:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}.wrap{padding-bottom:110px}#hintbar span{font-size:.9rem}#hintbar button{flex:none}
.hint{background:#ffe0b2;color:#3b2a10}.hint q{font-style:italic}
@media (prefers-reduced-motion:reduce){.side,.side.open,button.side-tab{transition:none}}
"""

def student_panel(viewer, teacher=None):
    left, cfg = hints_left(viewer), settings()
    if teacher:
        wallet = ("<div class='card hintbox'><h3>Your hints <span class=star>∞</span></h3><p><b>Unlimited</b> (teacher).</p>"
                  "<p class=mut>On any marked paper, highlight part of the writing and tap <b>Get a hint</b> to see what a "
                  "student would get.</p></div>")
        return """<aside class="side left" id=side-student aria-label="Cheat sheet" aria-hidden=true>
<h2>Cheat Sheet</h2><p class=mut>The legal kind.</p>""" + wallet + STUDENT_TOOLS
    stars = "".join("<span class=star>★</span>" if i < left else "<span class='star off'>☆</span>" for i in range(MAX_HINTS))
    wallet = (f"<div class='card hintbox'><h3>Your hints {stars}</h3><p><b>{left} of {MAX_HINTS}</b> left.</p>"
              "<p class=mut>On a marked paper, highlight a part of your own writing and tap <b>Get a hint</b>. "
              "A hint points you in the right direction; it never gives you the answer.</p>"
              f"<p class=mut>Score <b>{cfg['hint_score']}%</b> or more on a marked paper to earn another hint "
              f"(you can hold up to {MAX_HINTS}).</p></div>")
    return """<aside class="side left" id=side-student aria-label="Cheat sheet" aria-hidden=true>
<h2>Cheat Sheet</h2><p class=mut>The legal kind.</p>""" + wallet + STUDENT_TOOLS

STUDENT_TOOLS = """<p class=mut>The tools below run in your own browser. Nothing you type in them is sent anywhere.</p>

<details class=tool open><summary>Draft check</summary><div>
<p class=mut id=dc-src>Checks the draft in the big box as you type.</p>
<textarea id=dc-text placeholder="Paste text here to check it" hidden></textarea>
<div class="out kv" id=dc-out></div></div></details>

<details class=tool><summary>Citation maker</summary><div>
<label>Style <select id=ci-style><option value=apa>APA 7</option><option value=mla>MLA 9</option></select></label>
<label>Author(s): Last, First; separate several with ; <input id=ci-author placeholder="Smith, Jane; Lee, Sam"></label>
<label>Title of the page or article <input id=ci-title></label>
<label>Website or publisher <input id=ci-site placeholder="National Geographic"></label>
<label>Date published <input id=ci-date type=date></label>
<label>Link (URL) <input id=ci-url type=url placeholder="https://"></label>
<label>Date you read it (MLA) <input id=ci-acc type=date></label>
<div class=out id=ci-out>Fill in what you know; leave the rest empty.</div><button type=button id=ci-copy>Copy citation</button></div></details>

<details class=tool><summary>Before you hand it in</summary><div class=checks id=hk>
<label><input type=checkbox>My main point is clear in the first paragraph.</label>
<label><input type=checkbox>Every fact, date and number has a source.</label>
<label><input type=checkbox>I explained things in my own words.</label>
<label><input type=checkbox>Each paragraph sticks to one idea.</label>
<label><input type=checkbox>I fixed what the marking sheet flagged.</label>
<label><input type=checkbox>I read it out loud once to catch mistakes.</label>
<label><input type=checkbox>Names, spelling and numbers are checked.</label>
<label><input type=checkbox>My sources list is at the end.</label>
<button type=button id=hk-reset class=link>Clear the ticks</button></div></details>

<details class=tool><summary>Unit converter</summary><div>
<label>What <select id=uc-kind></select></label>
<div class=cv><label>From <select id=uc-from></select></label><label>To <select id=uc-to></select></label></div>
<label>Amount <input id=uc-val type=number step=any value=1></label>
<div class=out id=uc-out></div></div></details>

<details class=tool><summary>Study timer</summary><div>
<div class=timer id=st-show>25:00</div><p class="mut" id=st-mode style=text-align:center>Focus: 25 minutes, then a 5 minute break.</p>
<div class=row><button type=button id=st-go>Start</button><button type=button id=st-reset class=link>Reset</button></div></div></details>
</aside>
<button class="side-tab left" type=button aria-controls=side-student aria-expanded=false aria-label="Open the cheat sheet">
<span class=tab-arrow aria-hidden=true></span><span class=tab-txt>Cheat Sheet</span><span class=tab-arrow aria-hidden=true></span></button>"""

DRAWER_JS = r"""<script>
(()=>{
const $=id=>document.getElementById(id);
const store={get(k,d){try{const v=localStorage.getItem('hw-'+k);return v===null?d:JSON.parse(v)}catch(e){return d}},
  set(k,v){try{localStorage.setItem('hw-'+k,JSON.stringify(v))}catch(e){}}};
// drawers
function setOpen(p,open){const t=document.querySelector(`[aria-controls="${p.id}"]`);p.classList.toggle('open',open);
  p.setAttribute('aria-hidden',!open);if(t)t.setAttribute('aria-expanded',open);}
document.querySelectorAll('.side-tab').forEach(t=>t.addEventListener('click',()=>{const p=$(t.getAttribute('aria-controls'));
  const open=!p.classList.contains('open');document.querySelectorAll('.side.open').forEach(o=>setOpen(o,false));setOpen(p,open);}));
document.addEventListener('keydown',ev=>{if(ev.key==='Escape')document.querySelectorAll('.side.open').forEach(o=>setOpen(o,false))});
document.querySelectorAll('.side[data-open]').forEach(p=>setOpen(p,true));
// draft check
const main=document.querySelector('form textarea[name=text]'),own=$('dc-text');
if(!main){own.hidden=false;$('dc-src').textContent='Paste any text to check it.'}
const stop=new Set('the a an and or but of to in on at for with is are was were be been it its this that these those as by from i you he she we they my your his her our their not no so if then than there here what which who when where how all any can could would should will just also into about over more most very much such do does did have has had me him them us'.split(' '));
const syl=w=>{w=w.toLowerCase().replace(/[^a-z]/g,'');if(w.length<=3)return 1;w=w.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/,'').replace(/^y/,'');const m=w.match(/[aeiouy]{1,2}/g);return m?m.length:1};
function draftCheck(){const t=(main||own).value.trim(),out=$('dc-out');
  if(!t){out.innerHTML='<span class=mut>Nothing to check yet.</span>';return}
  const words=t.match(/[A-Za-z0-9'’-]+/g)||[],sents=t.split(/(?<=[.!?])\s+/).filter(s=>/[A-Za-z]/.test(s)),paras=t.split(/\n\s*\n/).filter(p=>p.trim());
  const sy=words.reduce((a,w)=>a+syl(w),0),wps=words.length/Math.max(1,sents.length);
  const grade=Math.max(1,Math.round(0.39*wps+11.8*sy/Math.max(1,words.length)-15.59));
  const long=sents.map(s=>[s,(s.match(/[A-Za-z0-9'’-]+/g)||[]).length]).sort((a,b)=>b[1]-a[1])[0]||['',0];
  const freq={};words.forEach(w=>{w=w.toLowerCase().replace(/[’']s$/,'');if(w.length>3&&!stop.has(w))freq[w]=(freq[w]||0)+1});
  const top=Object.entries(freq).filter(([,n])=>n>=4).sort((a,b)=>b[1]-a[1]).slice(0,5);
  const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  out.innerHTML=`<b>Words</b><span>${words.length}</span><b>Sentences</b><span>${sents.length}</span><b>Paragraphs</b><span>${paras.length}</span>`+
   `<b>Reading time</b><span>about ${Math.max(1,Math.round(words.length/238))} min</span><b>Reading level</b><span>grade ${grade}</span>`+
   `<b>Avg sentence</b><span>${wps.toFixed(1)} words${wps>25?' (try some shorter ones)':''}</span>`+
   (long[1]>30?`<b>Longest</b><span>${long[1]} words: “${esc(long[0].slice(0,90))}${long[0].length>90?'…':''}” Could it be two sentences?</span>`:'')+
   (top.length?`<b>Used a lot</b><span>${top.map(([w,n])=>esc(w)+' ×'+n).join(', ')}</span>`:'');}
(main||own).addEventListener('input',draftCheck);draftCheck();
// citation maker
const months=['January','February','March','April','May','June','July','August','September','October','November','December'];
const mlaMon=['Jan.','Feb.','Mar.','Apr.','May','June','July','Aug.','Sept.','Oct.','Nov.','Dec.'];
const v=id=>$(id).value.trim(),escH=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function people(style){const a=v('ci-author').split(';').map(s=>s.trim()).filter(Boolean).map(p=>{const[l,f='']=p.split(',').map(s=>s.trim());return{l,f}});
  if(!a.length)return'';
  if(style==='apa'){const ini=f=>f.split(/[\s-]+/).filter(Boolean).map(x=>x[0].toUpperCase()+'.').join(' ');
    const n=a.map(p=>p.f?`${p.l}, ${ini(p.f)}`:p.l);return n.length<2?n[0]:n.length===2?`${n[0]}, & ${n[1]}`:`${n.slice(0,-1).join(', ')}, & ${n[n.length-1]}`;}
  const first=a[0].f?`${a[0].l}, ${a[0].f}`:a[0].l;return a.length===1?first:a.length===2?`${first}, and ${a[1].f?a[1].f+' ':''}${a[1].l}`:`${first}, et al`;}
function cite(){const st=v('ci-style')||$('ci-style').value,title=v('ci-title'),site=v('ci-site'),url=v('ci-url'),d=v('ci-date'),acc=v('ci-acc');
  let html='',text='';const dd=d?new Date(d+'T12:00'):null,ad=acc?new Date(acc+'T12:00'):null,au=people($('ci-style').value);
  if($('ci-style').value==='apa'){const date=dd?`(${dd.getFullYear()}, ${months[dd.getMonth()]} ${dd.getDate()})`:'(n.d.)';
    const head=au?`${au}${au.endsWith('.')?'':'.'} ${date}.`:'';
    html=(au?`${escH(head)} <i>${escH(title)}</i>.`:`<i>${escH(title)}</i>. ${date}.`)+(site?` ${escH(site)}.`:'')+(url?` ${escH(url)}`:'');
    text=(au?`${head} ${title}.`:`${title}. ${date}.`)+(site?` ${site}.`:'')+(url?` ${url}`:'');}
  else{const md=x=>`${x.getDate()} ${mlaMon[x.getMonth()]} ${x.getFullYear()}`;
    const parts=[site?`<i>${escH(site)}</i>`:'',dd?md(dd):'',url?escH(url.replace(/^https?:\/\//,'')):''].filter(Boolean);
    const tparts=[site,dd?md(dd):'',url.replace(/^https?:\/\//,'')].filter(Boolean);
    html=(au?escH(au)+'. ':'')+(title?`“${escH(title)}.” `:'')+parts.join(', ')+(parts.length?'.':'')+(ad?` Accessed ${md(ad)}.`:'');
    text=(au?au+'. ':'')+(title?`“${title}.” `:'')+tparts.join(', ')+(tparts.length?'.':'')+(ad?` Accessed ${md(ad)}.`:'');}
  $('ci-out').innerHTML=title||site||url?html:'Fill in what you know; leave the rest empty.';$('ci-out').dataset.text=text;}
['ci-style','ci-author','ci-title','ci-site','ci-date','ci-url','ci-acc'].forEach(id=>$(id).addEventListener('input',cite));
$('ci-copy').addEventListener('click',()=>{const t=$('ci-out').dataset.text;if(!t)return;
  (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>{$('ci-copy').textContent='Copied!';setTimeout(()=>$('ci-copy').textContent='Copy citation',1500)},
  ()=>{const r=document.createRange();r.selectNodeContents($('ci-out'));getSelection().removeAllRanges();getSelection().addRange(r)});});
// checklist
const boxes=[...document.querySelectorAll('#hk input')],saved=store.get('checklist',[]);
boxes.forEach((b,i)=>{b.checked=!!saved[i];b.addEventListener('change',()=>store.set('checklist',boxes.map(x=>x.checked)))});
$('hk-reset').addEventListener('click',()=>{boxes.forEach(b=>b.checked=false);store.set('checklist',[])});
// unit converter
const U={'Length':{mm:.001,cm:.01,m:1,km:1000,in:.0254,ft:.3048,yd:.9144,mi:1609.344},
 'Mass':{mg:1e-6,g:.001,kg:1,t:1000,oz:.028349523125,lb:.45359237},
 'Temperature':{'°C':'C','°F':'F','K':'K'},
 'Energy':{J:1,kJ:1000,cal:4.184,kcal:4184,Wh:3600,kWh:3.6e6},
 'Speed':{'m/s':1,'km/h':1/3.6,mph:.44704,knot:.514444}};
const kind=$('uc-kind'),from=$('uc-from'),to=$('uc-to');
kind.innerHTML=Object.keys(U).map(k=>`<option>${k}</option>`).join('');
function fillUnits(){const ks=Object.keys(U[kind.value]);[from,to].forEach((s,i)=>{s.innerHTML=ks.map(k=>`<option>${k}</option>`).join('');s.selectedIndex=Math.min(i?2:1,ks.length-1)});conv()}
function conv(){const x=parseFloat($('uc-val').value),u=U[kind.value];if(isNaN(x)){$('uc-out').textContent='';return}let r;
  if(kind.value==='Temperature'){const c={C:x,F:(x-32)*5/9,K:x-273.15}[u[from.value]];r={C:c,F:c*9/5+32,K:c+273.15}[u[to.value]]}
  else r=x*u[from.value]/u[to.value];
  $('uc-out').textContent=`${x} ${from.value} = ${+r.toPrecision(6)} ${to.value}`}
kind.addEventListener('change',fillUnits);[from,to,$('uc-val')].forEach(e=>e.addEventListener('input',conv));fillUnits();
// hints: highlight part of a marked paper, then ask
const bar=$('hintbar'),go=$('hb-go');
if(go){const def=$('hb-text').textContent;
  const show=()=>{const sel=getSelection(),t=sel.toString().trim();let k=null;
    if(sel.rangeCount&&t.length>=3&&t.length<=400){const n=sel.getRangeAt(0).commonAncestorContainer;const p=(n.nodeType===1?n:n.parentElement).closest('.orig p[data-k]');if(p)k=p.dataset.k}
    if(k){$('hb-text').textContent='Hint on “'+(t.length>60?t.slice(0,60)+'…':t)+'”';go.disabled=false;bar.dataset.k=k;bar.dataset.t=t}
    else if(document.activeElement!==go){$('hb-text').textContent=t?'Highlight inside the writing itself (3 to 400 characters).':def;go.disabled=true}};
  document.addEventListener('selectionchange',()=>setTimeout(show,0));
  go.addEventListener('click',()=>{if(go.disabled)return;$('hb-k').value=bar.dataset.k;$('hb-t').value=bar.dataset.t;go.disabled=true;go.textContent='Asking…';$('hb-form').submit()});}
// study timer (keeps running across page reloads)
let tick=null;const FOCUS=25*60,BREAK=5*60,baseTitle=document.title;
function tstate(){return store.get('timer',{mode:'focus',end:0,left:FOCUS})}
function render(){const s=tstate(),left=s.end?Math.max(0,Math.round((s.end-Date.now())/1000)):s.left;
  const mm=String(Math.floor(left/60)).padStart(2,'0'),ss=String(left%60).padStart(2,'0');$('st-show').textContent=`${mm}:${ss}`;
  $('st-mode').textContent=s.mode==='focus'?'Focus time. Phone face down!':'Break time. Stretch, drink some water.';
  $('st-go').textContent=s.end?'Pause':'Start';document.title=s.end?`${mm}:${ss} · ${baseTitle}`:baseTitle;
  if(s.end&&left===0){const next=s.mode==='focus'?{mode:'break',end:Date.now()+BREAK*1000,left:BREAK}:{mode:'focus',end:0,left:FOCUS};store.set('timer',next);}}
function run(){clearInterval(tick);if(tstate().end)tick=setInterval(render,1000);render()}
$('st-go').addEventListener('click',()=>{const s=tstate();if(s.end){store.set('timer',{...s,end:0,left:Math.max(0,Math.round((s.end-Date.now())/1000))})}
  else store.set('timer',{...s,end:Date.now()+s.left*1000});run()});
$('st-reset').addEventListener('click',()=>{store.set('timer',{mode:'focus',end:0,left:FOCUS});run()});run();
})();
</script>"""

def teacher_panel(teacher, viewer, open_it=False, msg=""):
    if not teacher:
        body = (f"<h2>Staff room</h2><p class=mut>Teachers: log in to unlock teacher mode, which can also draft sourced "
                "additions and new sections. Students only ever get student mode.</p>"
                + (f"<div class='card add'><b class=hand>Note:</b> {e(msg)}</div>" if msg else "")
                + "<form method=post action=/teacher class=card><label class=mut>Email<input name=email type=email "
                "autocomplete=username required></label><label class=mut>Password<input name=password type=password "
                "autocomplete=current-password required></label><p><button>Log in</button></p></form>"
                "<p class=mut>No login yet? Your school adds teachers on the server with <code>add_teacher.py</code>.</p>")
    else:
        mine = [s for s in all_jobs() if not is_student(s) and (s.get("owner") == viewer or not s.get("owner"))]
        rows = "".join(f"<li><a href='/job/{s['id']}'>{e(s['title'])}</a> <span class=mut>· {STATUS.get(s['status'], e(s['status']))}"
                       f"{f' {progress(s)[0]}%' if s['status'] == 'running' else ''}</span></li>" for s in mine[:20])
        body = (f"<h2>Staff room</h2><p>Logged in as <b>{e(teacher)}</b></p>"
                "<form method=post action=/teacher/logout><button>Log out</button></form>"
                f"<h3 style='margin-top:1.2em'>Your teacher work</h3><ul>{rows or '<li class=mut>None yet.</li>'}</ul>"
                f"<h3>Student hints</h3><form method=post action=/teacher/settings class=card>"
                f"<label class=mut>Score needed to earn a hint (%)<input name=hint_score type=number min=0 max=100 value={settings()['hint_score']}></label>"
                f"<label class=mut>Hints a new student starts with (0-{MAX_HINTS})<input name=start_hints type=number min=0 max={MAX_HINTS} value={settings()['start_hints']}></label>"
                "<p><button>Save</button></p></form>"
                "<h3>How teacher mode works</h3><ul class=mut><li>Pick <b>Teacher</b> in the form before handing in.</li>"
                "<li>You get sourced additions and new sections as tick-box suggestions, then <b>Write out the clean copy</b>.</li>"
                "<li>Students never see teacher mode, and teacher work only opens while you are logged in.</li>"
                "<li>Logins last 30 days on this device. 3 wrong passwords lock that address out for 1 hour.</li></ul>")
    return (f"<aside class='side right' id=side-staff aria-label='Staff room' aria-hidden=true{' data-open' if open_it else ''}>{body}</aside>"
            "<button class='side-tab right' type=button aria-controls=side-staff aria-expanded=false aria-label='Open the staff room'>"
            "<span class=tab-arrow aria-hidden=true></span><span class=tab-txt>Staff room</span><span class=tab-arrow aria-hidden=true></span></button>")

def drawers(teacher, viewer, open_staff=False, msg=""):
    return student_panel(viewer, teacher) + teacher_panel(teacher, viewer, open_staff, msg) + DRAWER_JS

STATUS = {"queued": "In the pile", "running": "Being marked…", "done": "Marked ✓", "error": "Lost in the backpack"}
MARKS = {"supported": "✓ checks out", "partly": "½ partly right", "unclear": "? needs a source", "contradicted": "✗ check this"}

# step -> (share of the total time, what the student sees)
PHASES = {"sections": (0.01, "Reading your draft"), "claims": (0.06, "Finding the claims to check"),
          "research": (0.12, "Looking up sources"), "verdicts": (0.45, "Checking claims against sources"),
          "newsections": (0.18, "Looking for extra topic ideas"), "additions": (0.18, "Writing the teacher's notes")}

def progress(st):
    """(percent done, label) for a running job, from the step it is on and how far through that step it is."""
    secs = st.get("sections", [])
    claims = [c for x in secs for c in x.get("claims", [])]
    step, done_share = st["step"], 0.0
    for name, (share, _) in PHASES.items():
        if name == step:
            break
        done_share += share
    part = {"claims": lambda: (sum("claims" in x for x in secs), len(secs)),
            "research": lambda: (sum("evidence" in c for c in claims), len(claims)),
            "verdicts": lambda: (sum("verdict" in c for c in claims), len(claims)),
            "newsections": lambda: (sum(("tips" in x or "draft" in x) for x in st.get("new", [])), len(st.get("new", [])) or 1),
            "additions": lambda: (sum("additions" in x for x in secs), len(secs))}.get(step, lambda: (0, 1))()
    n, total = part
    share, label = PHASES.get(step, (0, step))
    pct = min(99, int(100 * (done_share + share * (n / total if total else 0))))
    return pct, f"{label}" + (f": {n} of {total}" if step in ("research", "verdicts") and total else "")

def progress_bar(st):
    if st["status"] == "queued":
        place = queue_place(st)
        return (f"<div class=progress><div class=bar><i style='width:0%'></i></div><div class=plabel>Waiting in the pile"
                f"{f' (you are number {place})' if place else ''}. Marking starts when the papers ahead of yours are done.</div></div>")
    pct, label = progress(st)
    took = time.time() - st.get("started", st["created"])
    left = f" · roughly {max(1, round(took / pct * (100 - pct) / 60))} min left" if pct >= 8 else ""
    return (f"<div class=progress><div class=bar role=progressbar aria-valuenow={pct} aria-valuemin=0 aria-valuemax=100>"
            f"<i style='width:{pct}%'></i></div><div class=plabel>✏ {e(label)} · <b>{pct}%</b>{left}</div></div>")

def queue_place(st):
    if st["status"] != "queued":
        return 0
    return 1 + sum(1 for s in all_jobs() if s["status"] in ("queued", "running") and s["created"] < st["created"])

def home(viewer, teacher, msg="", staff_msg=""):
    mine = [s for s in all_jobs() if s.get("owner") == viewer or (teacher and not s.get("owner"))]
    rows = "".join(
        f"<tr><td><a href='/job/{s['id']}'>{e(s['title'])}</a></td><td>{'teacher' if not is_student(s) else 'student'}</td>"
        f"<td>{STATUS.get(s['status'], e(s['status']))}{f' {progress(s)[0]}%' if s['status'] == 'running' else ''}</td>"
        f"<td class=mut>{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['created']))}</td></tr>" for s in mine)
    mode_pick = ("<select name=mode><option value=teacher>Teacher: draft sourced additions</option>"
                 "<option value=student>Student: check and coach</option></select>") if teacher else ""
    return page("Homework Wizard", f"""<h1>{HAT}<span class=hl>Homework Wizard</span></h1>
<div class=motto>It checks your homework. It doesn't do it for you.</div>
<p class=mut>Hand in your own draft. Every claim gets checked against real sources, and your work comes back marked:
what checks out, what to fix, and notes on how to make it stronger, with the sources to read. Marking takes
20-60 minutes and papers are marked one at a time, so you can close this page and come back.</p>
{f"<div class='card add'><b class=hand>Note:</b> {e(msg)}</div>" if msg else ""}
<form method=post action=/new class=card>
<div class=row><input name=title placeholder="Title of your assignment" required>
<label class=num>Extra topic ideas <input name=extra type=number min=0 max=6 value=3></label>{mode_pick}</div>
<p><textarea name=text placeholder="Paste your draft here. Put each heading on its own line." required></textarea></p>
<p><button>✏️ Hand it in</button></p></form>
<h2>Your homework pile</h2><table>{rows or '<tr><td class=mut>Nothing handed in yet (only work handed in from this browser shows here).</td></tr>'}</table>
""", side=drawers(teacher, viewer, bool(staff_msg), staff_msg))

TEACHER_SCRIPT = """<script>
const W=t=>(t.match(/\\b\\w[\\w'’-]*\\b/g)||[]).length;
function picked(){return [...document.querySelectorAll('.pick:checked')].map(c=>c.closest('[data-k]'))}
function count(){let n=W([...document.querySelectorAll('.orig p[data-k]')].map(p=>p.innerText).join(' '));
for(const b of picked())n+=W([...b.querySelectorAll('.txt,.hd')].map(p=>p.innerText).join(' '));
document.getElementById('nw').textContent=n;document.getElementById('nm').textContent=Math.max(1,Math.round(n/238));}
if(document.getElementById('nw'))document.addEventListener('change',count);
function build(){const src=[],idx=new Map();
const fix=el=>{let t=el.innerText;el.querySelectorAll('a').forEach(a=>{if(!idx.has(a.href)){src.push(a.href);idx.set(a.href,src.length)}
t=t.replace('['+a.textContent+']','[S'+idx.get(a.href)+']').replace(a.textContent+',','S'+idx.get(a.href)+',').replace(','+a.textContent+']',',S'+idx.get(a.href)+']')});return t.replace(/\\[S/g,'[').replace(/,S/g,',')};
const parts=[],news=[...document.querySelectorAll('.newsec[data-k]')].filter(b=>b.querySelector('.pick').checked);
const secs=[...document.querySelectorAll('.orig')];
secs.forEach((o,i)=>{const h=o.previousElementSibling;if(h&&h.tagName==='H2')parts.push(h.innerText+':');
const kids=[...o.children];kids.forEach((k,j)=>{const last=i===secs.length-1&&j===kids.length-1&&k.tagName==='P';
if(last)news.forEach(n=>{parts.push(n.querySelector('.hd').innerText+':');n.querySelectorAll('.txt').forEach(p=>parts.push(fix(p)))});
if(k.tagName==='P')parts.push(k.innerText);else if(k.querySelector('.pick').checked)parts.push(fix(k.querySelector('.txt')))})});
if(src.length)parts.push('Sources:\\n'+src.map((s,i)=>'['+(i+1)+'] '+s).join('\\n'));
document.getElementById('out').value=parts.join('\\n\\n');}
</script>"""

def report(st, viewer=None, teacher=None):
    running = st["status"] in ("queued", "running")
    hinting = any(h.get("status") == "pending" for h in st.get("hints", []))
    can_hint = st["status"] == "done" and (bool(teacher) or (is_student(st) and st.get("owner") == viewer))
    left = "unlimited" if teacher else hints_left(viewer)
    student = is_student(st)
    orig_words = words(st["text"])
    place = queue_place(st)
    claims = [c for s in st.get("sections", []) for c in s.get("claims", []) if "verdict" in c]
    tally = {v: sum(1 for c in claims if c["verdict"] == v) for v in MARKS}
    score = (f"<div class=score>{tally['supported']}/{len(claims)} check out</div> <span class=mut>"
             + " · ".join(f"{MARKS[v]}: {tally[v]}" for v in ("partly", "unclear", "contradicted")) + "</span>") if claims else ""
    head = ((progress_bar(st) if running else "") + f"<p><a href='/'>← Back to the pile</a></p><h1>{e(st['title'])}</h1>{score}"
            f"<div class=stats><span>{STATUS.get(st['status'], e(st['status']))}"
            f"{f' · you are number {place} in the pile' if place else ''}</span>"
            f"<span>Original: {orig_words} words · {read_min(orig_words)} min</span>"
            + ("" if student else f"<span>With selected: <span id=nw>{orig_words}</span> words · <span id=nm>{read_min(orig_words)}</span> min</span>")
            + "</div>")
    award = st.get("hint_award")
    if award and st.get("owner") == viewer:
        head += (f"<div class='card add'><b class=hand>{'Nice work!' if award['earned'] else 'Marked.'}</b> You scored {award['score']}%"
                 + (" and earned a hint! Open the Cheat Sheet to see your hints." if award["earned"]
                    else f". Score {award.get('needed', settings()['hint_score'])}% or more to earn a hint." if "needed" in award
                    else ", but your hint wallet was already full.") + "</div>")
    if can_hint:
        head += (f"<p class=mut>Stuck on something? Highlight part of the writing below and tap <b>Get a hint</b> "
                 f"({'teachers get unlimited hints' if teacher else f'you have {left} of {MAX_HINTS}'}).</p>")
    if st["status"] == "error":
        head += f"<div class=card><b>Something went wrong:</b> {e(st.get('error'))} <form method=post action='/retry/{st['id']}'><button>Try again</button></form></div>"
    body = ""
    for si, s in enumerate(st.get("sections", [])):
        body += f"<h2>{e(s['title'])}</h2>" if si else ""
        adds = {}
        for ai, a in enumerate(s.get("additions", [])):
            try:
                after = int(a.get("after", len(s["paras"])))
            except (TypeError, ValueError):
                after = len(s["paras"])
            adds.setdefault(min(max(after, 1), len(s["paras"])), []).append((ai, a))
        body += "<div class=orig>"
        for pi, p in enumerate(s["paras"], 1):
            body += f"<p data-k='o{si}_{pi}'>{e(p)}</p>"
            for h in st.get("hints", []):
                if h["k"] == f"o{si}_{pi}":
                    body += (f"<div class='card add hint'><b class=hand>Hint</b> <span class=mut>for <q>{e(h['text'][:120])}</q></span>"
                             + (f"<p>{cite_links(h['answer'], h.get('evidence', []))}</p>" if h.get("answer")
                                else "<p class=mut>Thinking about it… this can take a minute or two.</p>") + "</div>")
            for ai, a in adds.get(pi, []):
                if student:
                    body += (f"<div class='card add'><b class=hand>Teacher's note</b> <span class=mut>— {e(a.get('why'))}</span>"
                             f"<p>{cite_links(a['text'], s.get('add_evidence', []))}</p></div>")
                else:
                    body += (f"<div class='card add pickable' data-k='a{si}_{ai}'><label><input type=checkbox class=pick>Suggested addition"
                             f" <span class=mut>— {e(a.get('why'))}</span></label><p class=txt>{cite_links(a['text'], s.get('add_evidence', []))}</p></div>")
        body += "</div>"
        if s.get("claims"):
            items = ""
            for c in s["claims"]:
                if "verdict" not in c:
                    items += f"<li><span class='v pending'>… marking</span>{e(c['claim'])}</li>"; continue
                fix = f"<br><b>More accurate:</b> {cite_links(c['correction'], c['evidence'])}" if c.get("correction") else ""
                srcs = " · ".join(f"<a href='{e(x['url'])}' target=_blank>[{i+1}] {e(urllib.parse.urlparse(x['url']).netloc)}</a>"
                                  for i, x in enumerate(c["evidence"]))
                items += (f"<li><span class='v {e(c['verdict'])}'>{MARKS.get(c['verdict'], e(c['verdict']))}</span><b>{e(c['claim'])}</b><br>"
                          f"{cite_links(c['note'], c['evidence'])}{fix}<div class=src>{srcs}</div></li>")
            body += f"<details class='card claims' {'open' if si else ''}><summary><b>Marking sheet</b> ({len(s['claims'])} claims checked)</summary><ul>{items}</ul></details>"
    for ni, x in enumerate(st.get("new", [])):
        srcs = "<br>".join(f"[{i+1}] <a href='{e(v['url'])}' target=_blank>{e(v['title'] or v['url'])}</a>" for i, v in enumerate(x.get("evidence", [])))
        if student:
            tips = "".join(f"<li>{cite_links(t.lstrip('-* ').strip(), x.get('evidence', []))}</li>"
                           for t in (x.get("tips") or "").splitlines() if t.strip())
            body += (f"<div class='card newsec'><b>Idea for extra credit: {e(x['title'])}</b><p class=mut>{e(x.get('angle'))}</p>"
                     + (f"<ul>{tips}</ul><details class=src><summary>Sources to read</summary>{srcs}</details>" if tips else "<p class=mut>(researching…)</p>")
                     + "</div>")
        elif "draft" not in x:
            body += f"<div class='card newsec'><b>New section planned:</b> {e(x['title'])} <span class=mut>(drafting…)</span></div>"
        else:
            body += (f"<div class='card newsec' data-k='n{ni}'><label><input type=checkbox class=pick>Suggested new section</label>"
                     f"<h3 class=hd>{e(x['title'])}</h3><p class=mut>{e(x.get('angle'))}</p>"
                     + "".join(f"<p class=txt>{cite_links(p, x['evidence'])}</p>" for p in x["draft"].split("\n\n") if p.strip())
                     + f"<details class=src><summary>Sources</summary>{srcs}</details></div>")
    tail = ""
    if not student and st["status"] == "done":
        tail = """<h2>Clean copy</h2><p class=mut>Tick the suggestions you want above, then write it out. New sections go before your closing paragraph.
Citation numbers become a source list at the end.</p><p><button onclick=build()>✏️ Write out the clean copy</button></p><textarea id=out></textarea>"""
    logs = f"<details class=card {'open' if running else ''}><summary>Teacher's scratch pad (progress log)</summary><pre>{e(chr(10).join(st.get('log', [])[-60:]))}</pre></details>"
    if can_hint and (teacher or left > 0):
        bar = ("<div id=hintbar class=show role=region aria-label='Hints'><span id=hb-text>Highlight part of the writing above to get a hint.</span>"
               f"<button type=button id=hb-go disabled>Get a hint{'' if teacher else f' ({left} left)'}</button></div>"
               f"<form id=hb-form method=post action='/hint/{st['id']}' hidden><input name=k id=hb-k><input name=text id=hb-t></form>")
    elif can_hint:
        bar = (f"<div id=hintbar class='show nohints' role=region aria-label='Hints'><span>No hints left. Score {settings()['hint_score']}% "
               "or more on a marked paper to earn another.</span></div>")
    elif is_student(st) and st.get("owner") == viewer or teacher:
        bar = "<div id=hintbar class='show nohints' role=region aria-label='Hints'><span>Hints unlock when marking is finished.</span></div>"
    else:
        bar = ""
    return page(st["title"], head + body + tail + logs + bar + ("" if student else TEACHER_SCRIPT), refresh=running or hinting,
                side=drawers(teacher, viewer))


class H(BaseHTTPRequestHandler):
    def cookies(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        return {k: v.value for k, v in c.items()}

    def who(self):
        """(browser id, teacher name or None). New browsers get an id cookie so they only see their own jobs."""
        c = self.cookies()
        vid = c.get("hw_id") if re.fullmatch(r"[0-9a-f]{32}", c.get("hw_id", "")) else None
        self.new_cookies = [] if vid else [f"hw_id={(vid := secrets.token_hex(16))}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"]
        return vid, session_teacher(c.get("hw_teacher"))

    def send(self, code, body, ctype="text/html; charset=utf-8", loc=None, cookies=()):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        if loc:
            self.send_header("Location", loc)
        for c in list(getattr(self, "new_cookies", [])) + list(cookies):
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)

    def job(self, jid, teacher):
        """A job is reachable by its unguessable link; teacher-mode jobs only by signed-in teachers."""
        if not re.fullmatch(r"[0-9a-f]{12,32}", jid) or not os.path.exists(jpath(jid)):
            return None
        st = load(jid)
        return st if (is_student(st) or teacher) else None

    def do_GET(self):
        vid, teacher = self.who()
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(200, home(vid, teacher))
        m = re.match(r"^/job/([0-9a-f]+)(\.json)?$", p)
        st = self.job(m.group(1), teacher) if m else None
        if st:
            return self.send(200, json.dumps(st, indent=1), "application/json") if m.group(2) else self.send(200, report(st, vid, teacher))
        self.send(404, page("Not found", "<p>Not found. <a href='/'>Home</a></p>"))

    def do_POST(self):
        vid, teacher = self.who()
        n = min(int(self.headers.get("Content-Length", 0)), 400_000)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
        f = lambda k, d="": form.get(k, [d])[0]
        if self.path == "/teacher":
            ip = (self.headers.get(IP_HEADER, "") if IP_HEADER else "").strip() or self.client_address[0]
            if lock_left(ip):
                print(f"login refused (locked out) for {f('email')[:100]!r} from {ip}", flush=True)
                return self.send(429, home(vid, None, staff_msg=f"Too many wrong passwords. Try again in {lock_left(ip) // 60 + 1} minutes."))
            name = check_login(f("email"), f("password"))
            print(f"login {'ok' if name else 'FAILED'} for {f('email')[:100]!r} from {ip}", flush=True)
            if not name:
                login_failed(ip); time.sleep(1.5)
                left = LOCK_ATTEMPTS - len(failed.get(ip, []))
                return self.send(403, home(vid, None, staff_msg="Wrong email or password." + (
                    f" {left} attempt{'s' if left > 1 else ''} left before a 1-hour lockout." if not lock_left(ip)
                    else " Locked out for 1 hour.")))
            failed.pop(ip, None)
            return self.send(303, "", loc="/", cookies=[f"hw_teacher={make_session(name)}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"])
        if self.path == "/teacher/settings" and teacher:
            cfg = settings()
            try:
                cfg["hint_score"] = max(0, min(100, int(f("hint_score", cfg["hint_score"]))))
                cfg["start_hints"] = max(0, min(MAX_HINTS, int(f("start_hints", cfg["start_hints"]))))
            except ValueError:
                pass
            write_json(SETTINGS_FILE, cfg)
            return self.send(303, "", loc="/")
        m = re.match(r"^/hint/([0-9a-f]+)$", self.path)
        if m:
            st = self.job(m.group(1), teacher)
            k, text = f("k"), " ".join(f("text").split())[:400]
            ok = (st and st["status"] == "done" and (teacher or (is_student(st) and st.get("owner") == vid)) and re.fullmatch(r"o\d+_\d+", k)
                  and len(text) >= 3 and text in " ".join(st["text"].split()))
            if not ok:
                return self.send(400, page("Hint", "<p>That can't get a hint. Highlight part of your own writing on a marked paper. <a href='/'>Back</a></p>"))
            if not teacher and change_hints(vid, -1) is None:
                return self.send(303, "", loc=f"/job/{st['id']}")
            with file_lock:
                st = load(st["id"])
                st.setdefault("hints", []).append({"k": k, "text": text, "status": "pending", "asked": time.time(),
                                                   "paid_by": None if teacher else vid})
                save(st)
            threading.Thread(target=make_hint, args=(st["id"], len(st["hints"]) - 1), daemon=True).start()
            return self.send(303, "", loc=f"/job/{st['id']}")
        if self.path == "/teacher/logout":
            return self.send(303, "", loc="/", cookies=["hw_teacher=; Path=/; Max-Age=0"])
        if self.path == "/new":
            text = f("text").replace("\r\n", "\n").strip()
            if not text:
                return self.send(400, home(vid, teacher, "Paste your draft first."))
            mode = "teacher" if teacher and f("mode") == "teacher" else "student"
            if not teacher and any(s.get("owner") == vid and s["status"] in ("queued", "running") for s in all_jobs()):
                return self.send(429, home(vid, teacher, "You already have a job waiting or running. Please wait for it to finish."))
            jid = secrets.token_hex(16)
            os.makedirs(os.path.join(DATA, jid))
            try:
                extra = max(0, min(6, int(f("extra", "3") or 3)))
            except ValueError:
                extra = 3
            st = {"id": jid, "title": f("title", "Untitled")[:200] or "Untitled", "text": text[:60000], "created": time.time(),
                  "extra": extra, "status": "queued", "step": "sections", "mode": mode, "owner": vid}
            save(st); wake.set()
            return self.send(303, "", loc=f"/job/{jid}")
        m = re.match(r"^/retry/([0-9a-f]+)$", self.path)
        st = self.job(m.group(1), teacher) if m else None
        if st and st["status"] == "error":
            st["status"] = "queued"; save(st); wake.set()
            return self.send(303, "", loc=f"/job/{st['id']}")
        self.send(404, "not found")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    print(f"Homework Wizard on port {PORT}; {len(read_keys())} teacher login(s) in {KEYS}", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
