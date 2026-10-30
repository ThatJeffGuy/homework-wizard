#!/usr/bin/env python3
"""Homework Wizard: a student's own draft in, a fact check and coaching report out. Fully local.

It checks work, it does not write it: every suggestion is a pointer (what to look into, which source says so),
never text that could be pasted in.

Teacher mode also drafts sourced additions and new sections. Only signed-in teachers get it: the school creates a key
per teacher with  python3 homework_wizard.py teacher-key add "Name"  (see README).

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
KEYS = os.environ.get("KEYS_FILE", os.path.join(_base, "teachers.json"))       # hashed teacher keys
SECRET_FILE = os.environ.get("SECRET_FILE", os.path.join(_base, "secret.key"))  # signs teacher sessions
SESSION_DAYS = 30


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
    try:
        while st["step"] != "done":
            st["status"] = "running"; log(st, "step: " + st["step"])
            {"sections": step_sections, "claims": step_claims, "research": step_research, "verdicts": step_verdicts,
             "newsections": step_newsections, "additions": step_additions}[st["step"]](st)
            st["step"] = STEPS[STEPS.index(st["step"]) + 1]; save(st)
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


# ---------- teacher keys + sessions ----------
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

def key_hash(key):
    return hashlib.sha256(("homework-wizard-teacher:" + key.strip()).encode()).hexdigest()

def teacher_for_key(key):
    h = key_hash(key)
    for name, rec in read_keys().items():
        if hmac.compare_digest(rec["hash"], h):
            return name
    return None

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

def teacher_key_cli(args):
    usage = ('usage: homework_wizard.py teacher-key add "Name" | remove "Name" | list\n'
             f"(keys file: {KEYS}; set DATA_DIR / KEYS_FILE the same way as for the server)")
    keys = read_keys()
    if args[:1] == ["add"] and len(args) == 2:
        name = args[1].strip()
        if not name or name in keys:
            sys.exit(f"'{name}' already has a key; remove it first to make a new one." if name else usage)
        key = "-".join(secrets.token_hex(3).upper() for _ in range(5))   # 120 random bits
        keys[name] = {"hash": key_hash(key), "created": time.strftime("%Y-%m-%d")}
        write_keys(keys)
        print(f"Teacher key for {name} (shown only once, give it to them privately):\n\n    {key}\n")
    elif args[:1] == ["remove"] and len(args) == 2:
        if keys.pop(args[1].strip(), None) is None:
            sys.exit(f"no key for '{args[1]}'")
        write_keys(keys); print(f"removed {args[1]} (their sessions end too)")
    elif args == ["list"]:
        print("\n".join(f"{n}  (created {r['created']})" for n, r in sorted(keys.items())) or "no teacher keys yet")
    else:
        sys.exit(usage)


# ---------- web ----------
CSS = """
:root{--bg:#f7f6f2;--fg:#1d1d1b;--mut:#6b6a64;--card:#fff;--line:#e2e0d8;--acc:#2f6f5e;
--sup:#2e7d32;--par:#b7791f;--unc:#6b6a64;--con:#c0392b;--add:#e9f4ee;--new:#eef1fb}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--mut:#9c9a92;--card:#1f1f1c;--line:#34332e;
--acc:#6fc2a8;--add:#1d2b25;--new:#1d2130}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 80px}h1{font-size:1.6rem;margin:.2em 0}
h2{font-size:1.25rem;margin:2em 0 .4em;padding-top:.6em;border-top:1px solid var(--line)}
.mut{color:var(--mut);font-size:.9rem}a{color:var(--acc)}
textarea,input,select{width:100%;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
textarea{min-height:340px}button{font:inherit;padding:9px 18px;border:0;border-radius:8px;background:var(--acc);color:#fff;cursor:pointer}
button.link{background:none;color:var(--acc);padding:0;text-decoration:underline}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.row>*{flex:1 1 160px}
.v{display:inline-block;font-size:.75rem;font-weight:600;text-transform:uppercase;padding:1px 8px;border-radius:99px;color:#fff;margin-right:6px}
.supported{background:var(--sup)}.partly{background:var(--par)}.unclear{background:var(--unc)}.contradicted{background:var(--con)}
.claims{font-size:.92rem}.claims li{margin:.5em 0}.add{background:var(--add)}.newsec{background:var(--new)}
.add label,.newsec label{font-weight:600;font-size:.85rem;display:flex;gap:8px;align-items:center;cursor:pointer}
.src{font-size:.82rem}.src a{word-break:break-all}.orig p{margin:.7em 0}
pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,monospace;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:260px;overflow:auto}
footer{margin-top:48px;padding-top:12px;border-top:1px solid var(--line);color:var(--mut);font-size:.85rem;text-align:center}
.stats{display:flex;gap:18px;flex-wrap:wrap;font-weight:600}table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-bottom:1px solid var(--line)}
details.teacher{margin-top:28px}details.teacher form{display:flex;gap:10px;flex-wrap:wrap}details.teacher input{flex:1 1 220px;width:auto}
"""

FOOTER = (f"<footer><b>Made by <a href='https://www.everydaysciencestuff.com' target=_blank rel=noopener>That Scottish Guy</a></b>"
          f" · &copy; 2026 · <a href='{REPO}/blob/main/LICENSE' target=_blank rel=noopener>MIT</a>"
          f" · <a href='{REPO}' target=_blank rel=noopener>Source on GitHub</a></footer>")

def page(title, body, refresh=False):
    meta = '<meta http-equiv="refresh" content="20">' if refresh else ""
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"{meta}<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class=wrap>{body}{FOOTER}</div></body></html>")

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

def queue_place(st):
    if st["status"] != "queued":
        return 0
    return 1 + sum(1 for s in all_jobs() if s["status"] in ("queued", "running") and s["created"] < st["created"])

def home(viewer, teacher, msg=""):
    mine = [s for s in all_jobs() if s.get("owner") == viewer or (teacher and not s.get("owner"))]
    rows = "".join(
        f"<tr><td><a href='/job/{s['id']}'>{e(s['title'])}</a></td><td>{'teacher' if not is_student(s) else 'student'}</td>"
        f"<td>{e(s['status'])}{' · ' + e(s['step']) if s['status'] == 'running' else ''}</td>"
        f"<td class=mut>{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['created']))}</td></tr>" for s in mine)
    mode_pick = ("<select name=mode><option value=teacher>Teacher: draft sourced additions</option>"
                 "<option value=student>Student: check and coach</option></select>") if teacher else ""
    if teacher:
        who = (f"<p class=mut>Signed in as teacher: <b>{e(teacher)}</b> · <form method=post action=/teacher/logout "
               f"style=display:inline><button class=link>sign out</button></form></p>")
    else:
        who = ("<details class=teacher><summary class=mut>Teacher? Sign in with your key</summary>"
               "<form method=post action=/teacher class=card><input name=key type=password autocomplete=off "
               "placeholder='Teacher key' required><button>Sign in</button></form></details>")
    return page("Homework Wizard", f"""<h1>Homework Wizard</h1>
<p class=mut>Paste your own draft. A local model checks every claim against web sources and tells you what
holds up, what to fix and what could make it stronger, with the sources to read. It checks your work; it does not
write it. A full piece can take 20-60 minutes, and jobs run one at a time; you can close this page and come back.</p>
{f"<div class=card><b>{e(msg)}</b></div>" if msg else ""}
<form method=post action=/new class=card>
<div class=row><input name=title placeholder="Title" required><input name=extra type=number min=0 max=6 value=3 title="Extra topics">{mode_pick}</div>
<p class=mut>Number box = how many extra topics to suggest.</p>
<textarea name=text placeholder="Paste your draft here. Put headings on their own line." required></textarea>
<p><button>Start</button></p></form>
<h2>Your jobs</h2><table>{rows or '<tr><td class=mut>None yet (only jobs started from this browser are listed).</td></tr>'}</table>
{who}""")

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

def report(st):
    running = st["status"] in ("queued", "running")
    student = is_student(st)
    orig_words = words(st["text"])
    place = queue_place(st)
    head = (f"<p><a href='/'>← Homework Wizard</a></p><h1>{e(st['title'])}</h1>"
            f"<div class=stats><span>Status: {e(st['status'])}{' · ' + e(st['step']) if st['status'] == 'running' else ''}"
            f"{f' · number {place} in the queue' if place else ''}</span>"
            f"<span>Original: {orig_words} words · {read_min(orig_words)} min</span>"
            + ("" if student else f"<span>With selected: <span id=nw>{orig_words}</span> words · <span id=nm>{read_min(orig_words)}</span> min</span>")
            + "</div>")
    if st["status"] == "error":
        head += f"<div class=card><b>Error:</b> {e(st.get('error'))} <form method=post action='/retry/{st['id']}'><button>Resume</button></form></div>"
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
            for ai, a in adds.get(pi, []):
                if student:
                    body += (f"<div class='card add'><b>Tip</b> <span class=mut>— {e(a.get('why'))}</span>"
                             f"<p>{cite_links(a['text'], s.get('add_evidence', []))}</p></div>")
                else:
                    body += (f"<div class='card add' data-k='a{si}_{ai}'><label><input type=checkbox class=pick>Suggested addition"
                             f" <span class=mut>— {e(a.get('why'))}</span></label><p class=txt>{cite_links(a['text'], s.get('add_evidence', []))}</p></div>")
        body += "</div>"
        if s.get("claims"):
            items = ""
            for c in s["claims"]:
                if "verdict" not in c:
                    items += f"<li><span class='v unclear'>pending</span>{e(c['claim'])}</li>"; continue
                fix = f"<br><b>More accurate:</b> {cite_links(c['correction'], c['evidence'])}" if c.get("correction") else ""
                srcs = " · ".join(f"<a href='{e(x['url'])}' target=_blank>[{i+1}] {e(urllib.parse.urlparse(x['url']).netloc)}</a>"
                                  for i, x in enumerate(c["evidence"]))
                items += (f"<li><span class='v {e(c['verdict'])}'>{e(c['verdict'])}</span><b>{e(c['claim'])}</b><br>"
                          f"{cite_links(c['note'], c['evidence'])}{fix}<div class=src>{srcs}</div></li>")
            body += f"<details class='card claims' {'open' if si else ''}><summary><b>Fact check</b> ({len(s['claims'])} claims)</summary><ul>{items}</ul></details>"
    for ni, x in enumerate(st.get("new", [])):
        srcs = "<br>".join(f"[{i+1}] <a href='{e(v['url'])}' target=_blank>{e(v['title'] or v['url'])}</a>" for i, v in enumerate(x.get("evidence", [])))
        if student:
            tips = "".join(f"<li>{cite_links(t.lstrip('-* ').strip(), x.get('evidence', []))}</li>"
                           for t in (x.get("tips") or "").splitlines() if t.strip())
            body += (f"<div class='card newsec'><b>Topic you could add:</b> {e(x['title'])}<p class=mut>{e(x.get('angle'))}</p>"
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
        tail = """<h2>Final text</h2><p class=mut>Tick the suggestions you want above, then build. New sections go before your closing paragraph.
Citation numbers become a source list at the end.</p><p><button onclick=build()>Build final text</button></p><textarea id=out></textarea>"""
    logs = f"<details class=card {'open' if running else ''}><summary>Progress log</summary><pre>{e(chr(10).join(st.get('log', [])[-60:]))}</pre></details>"
    return page(st["title"], head + body + tail + logs + ("" if student else TEACHER_SCRIPT), refresh=running)


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
            return self.send(200, json.dumps(st, indent=1), "application/json") if m.group(2) else self.send(200, report(st))
        self.send(404, page("Not found", "<p>Not found. <a href='/'>Home</a></p>"))

    def do_POST(self):
        vid, teacher = self.who()
        n = min(int(self.headers.get("Content-Length", 0)), 400_000)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
        f = lambda k, d="": form.get(k, [d])[0]
        if self.path == "/teacher":
            name = teacher_for_key(f("key"))
            if not name:
                time.sleep(1.5)   # slows down guessing
                return self.send(403, home(vid, None, "That teacher key is not valid."))
            return self.send(303, "", loc="/", cookies=[f"hw_teacher={make_session(name)}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"])
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
    if sys.argv[1:2] == ["teacher-key"]:
        teacher_key_cli(sys.argv[2:])
        sys.exit()
    os.makedirs(DATA, exist_ok=True)
    print(f"Homework Wizard on port {PORT}; {len(read_keys())} teacher key(s) in {KEYS}", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
