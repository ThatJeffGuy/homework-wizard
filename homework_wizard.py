#!/usr/bin/env python3
"""Homework Wizard: a student's own draft in, a fact check and coaching report out. Fully local.

It checks work, it does not write it: every suggestion is a pointer (what to look into, which source says so),
never text that could be pasted in.

Teacher mode is an optional add-on, teacher_mode.py, distributed separately by the author (it is not in this
repository). Drop it next to this file and restart, and a mode picker appears.

Made by That Scottish Guy (https://www.everydaysciencestuff.com) - MIT licence, see LICENSE.

Pipeline per job (each step is saved to state.json, so a restart resumes where it stopped):
  sections -> claims -> research (SearXNG + page fetch) -> verdicts -> topic ideas -> tips -> done
The student's text is never rewritten.
"""
import hashlib, hmac, html, importlib, json, os, re, sys, threading, time, traceback, urllib.parse, urllib.request, uuid
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
ADDON = None   # optional teacher_mode.py add-on, loaded at start-up (see load_addon)
LICENSEE = ""
MODES = ["student"]


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
            ADDON.new_section(st, x, subject=subject, anchor=anchor, intro=intro, sample=sample)

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
        ADDON.additions(st, s, ev, notes, numbered)


def is_student(st):
    return st.get("mode", "student") == "student" or ADDON is None

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
textarea,input{width:100%;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
textarea{min-height:340px}button{font:inherit;padding:9px 18px;border:0;border-radius:8px;background:var(--acc);color:#fff;cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.row>*{flex:1 1 160px}
.v{display:inline-block;font-size:.75rem;font-weight:600;text-transform:uppercase;padding:1px 8px;border-radius:99px;color:#fff;margin-right:6px}
.supported{background:var(--sup)}.partly{background:var(--par)}.unclear{background:var(--unc)}.contradicted{background:var(--con)}
.claims{font-size:.92rem}.claims li{margin:.5em 0}.add{background:var(--add)}.newsec{background:var(--new)}
.add label,.newsec label{font-weight:600;font-size:.85rem;display:flex;gap:8px;align-items:center;cursor:pointer}
.src{font-size:.82rem}.src a{word-break:break-all}.orig p{margin:.7em 0}
pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,monospace;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:260px;overflow:auto}
footer{margin-top:48px;padding-top:12px;border-top:1px solid var(--line);color:var(--mut);font-size:.85rem}
.stats{display:flex;gap:18px;flex-wrap:wrap;font-weight:600}table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-bottom:1px solid var(--line)}
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

def home():
    rows = "".join(
        f"<tr><td><a href='/job/{s['id']}'>{e(s['title'])}</a></td><td>{e(s.get('mode', 'student'))}</td><td>{e(s['status'])}"
        f"{' · ' + e(s['step']) if s['status'] == 'running' else ''}</td>"
        f"<td class=mut>{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['created']))}</td></tr>" for s in all_jobs())
    mode_pick = "" if len(MODES) < 2 else ("<select name=mode>" + "".join(
        f"<option value={m}>{'Student: check and coach' if m == 'student' else e(ADDON.LABELS.get(m, m))}</option>"
        for m in MODES) + "</select>")
    return page("Homework Wizard", f"""<h1>Homework Wizard</h1>
<p class=mut>Paste your own draft. A local model checks every claim against web sources and tells you what
holds up, what to fix and what could make it stronger, with the sources to read. It checks your work; it does not
write it. On a CPU a full piece takes roughly 20-60 minutes; you can close this page.</p>
<form method=post action=/new class=card>
<div class=row><input name=title placeholder="Title" required><input name=extra type=number min=0 max=6 value=3 title="New sections">{mode_pick}</div>
<p class=mut>Number box = how many extra topics to suggest.</p>
<textarea name=text placeholder="Paste your draft here. Put headings on their own line." required></textarea>
<p><button>Start</button></p></form>
{f"<p class=mut>Teacher mode: licensed to {e(LICENSEE)}</p>" if LICENSEE else ""}
<h2>Jobs</h2><table>{rows or '<tr><td class=mut>None yet.</td></tr>'}</table>""")

def report(st):
    running = st["status"] in ("queued", "running")
    student = is_student(st)
    orig_words = words(st["text"])
    head = (f"<p><a href='/'>← Homework Wizard</a></p><h1>{e(st['title'])}</h1>"
            f"<div class=stats><span>Status: {e(st['status'])}{' · ' + e(st['step']) if running else ''}</span>"
            f"<span>Original: {orig_words} words · {read_min(orig_words)} min</span>"
            + ("" if student else ADDON.stats(orig_words))
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
                    continue
                body += ADDON.render_addition(si, ai, a, s)
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
        if student:
            srcs = "<br>".join(f"[{i+1}] <a href='{e(v['url'])}' target=_blank>{e(v['title'] or v['url'])}</a>" for i, v in enumerate(x.get("evidence", [])))
            tips = "".join(f"<li>{cite_links(t.lstrip('-* ').strip(), x.get('evidence', []))}</li>"
                           for t in (x.get("tips") or "").splitlines() if t.strip())
            body += (f"<div class='card newsec'><b>Topic you could add:</b> {e(x['title'])}<p class=mut>{e(x.get('angle'))}</p>"
                     + (f"<ul>{tips}</ul><details class=src><summary>Sources to read</summary>{srcs}</details>" if tips else "<p class=mut>(researching…)</p>")
                     + "</div>")
            continue
        body += ADDON.render_new(ni, x)
    tail = "" if student else ADDON.tail(st)
    logs = f"<details class=card {'open' if running else ''}><summary>Progress log</summary><pre>{e(chr(10).join(st.get('log', [])[-60:]))}</pre></details>"
    script = "" if student else ADDON.SCRIPT
    return page(st["title"], head + body + tail + logs + script, refresh=running)


class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8", loc=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        if loc:
            self.send_header("Location", loc)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(200, home())
        m = re.match(r"^/job/([0-9a-f]{12})(\.json)?$", p)
        if m and os.path.exists(jpath(m.group(1))):
            st = load(m.group(1))
            return self.send(200, json.dumps(st, indent=1), "application/json") if m.group(2) else self.send(200, report(st))
        self.send(404, page("Not found", "<p>Not found. <a href='/'>Home</a></p>"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        if self.path == "/new":
            text = form.get("text", [""])[0].replace("\r\n", "\n").strip()
            if not text:
                return self.send(400, "empty")
            jid = uuid.uuid4().hex[:12]
            os.makedirs(os.path.join(DATA, jid))
            st = {"id": jid, "title": form.get("title", ["Untitled"])[0][:200], "text": text, "created": time.time(),
                  "extra": max(0, min(6, int(form.get("extra", ["3"])[0] or 3))), "status": "queued", "step": "sections",
                  "mode": form.get("mode", [MODES[0]])[0] if form.get("mode", [MODES[0]])[0] in MODES else MODES[0]}
            save(st); wake.set()
            return self.send(303, "", loc=f"/job/{jid}")
        m = re.match(r"^/retry/([0-9a-f]{12})$", self.path)
        if m and os.path.exists(jpath(m.group(1))):
            st = load(m.group(1)); st["status"] = "queued"; save(st); wake.set()
            return self.send(303, "", loc=f"/job/{m.group(1)}")
        self.send(404, "not found")

    def log_message(self, *a):
        pass


# Public half of the teacher-mode signing key (RSA-3072, PKCS#1 v1.5, SHA-256). It can only CHECK licences;
# making one needs the private key, which only the author holds.
TEACHER_PUBKEY_N = int(
    "c684239c46a9a05fd03416e03dbfeb6a56d5c67a0d491bf183027d21bf9d5851c8c2154a64af3651ee20dcd03afeedf5"
    "d16935a45dfc32711034946a120cb806a1ebc696cec726710cb87a865fb9d4c0e4a1cbfa8815f7ebc16d2a466c8172f6"
    "4eac46a9032bf534e564f153e2b196fb23434a97ebc84e54b6f63e65642f9ebf66c6c3a0b41b7d611d4ed2c5f5f5ac98"
    "169e5c20db84f0d2eeb79584a66a8febeb4821ca7c4a98281e71b33d9438ffe516e7ca33fb89642b27ef7c04eb6dc093"
    "9558cd485b11c5d3414139093e1cb0533462d2142b8de60519728b534622ddf55ea8ea649fae8ed1fe9d0dfba79520f0"
    "4fe96a13b098221c0209647cc606de50cb306682debc78ac64ad3f1b87908eece69c8ee4954d0e53e45d752a2745a467"
    "7e198f25c8b4365588abef0f0a52cd326589867d03e72cd7a9c1c88a57b316cc757fcb48ac7b8a5add53de1d581c0132"
    "22eca66ece611b31956394d57f6dd872c62a59faeabcc8109eac3c0b9511e4bd6355964282f196efec4c1dab57644759"
    , 16)
LICENCE_RE = re.compile(r"^LICENCE = (\{.*\})\s*$", re.M)

def rsa_verify(payload, sig_hex):
    n = TEACHER_PUBKEY_N; k = (n.bit_length() + 7) // 8
    try:
        sig = int(sig_hex, 16)
    except (TypeError, ValueError):
        return False
    if not 0 < sig < n:
        return False
    info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(payload).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(info) - 3) + b"\x00" + info
    return hmac.compare_digest(pow(sig, 65537, n).to_bytes(k, "big"), expected)

def check_licence(source):
    """teacher_mode.py ends with a signed LICENCE line. The signature covers the licensee, the expiry date and a hash
    of the rest of the file, so it can't be forged, moved to another file, or kept after the file is edited."""
    m = LICENCE_RE.search(source)
    if not m:
        return None, "no licence in teacher_mode.py"
    try:
        lic = json.loads(m.group(1))
    except ValueError:
        return None, "unreadable licence"
    code_hash = hashlib.sha256(LICENCE_RE.sub("", source).encode()).hexdigest()
    payload = f"homework-wizard-teacher-v1\n{lic.get('to', '')}\n{lic.get('expires', '')}\n{code_hash}".encode()
    if not rsa_verify(payload, lic.get("sig", "")):
        return None, "licence signature is not valid (wrong key, or the file was changed)"
    if lic.get("expires") and time.strftime("%Y-%m-%d") > lic["expires"]:
        return None, f"licence expired on {lic['expires']}"
    return lic, ""

def load_addon():
    """If a licensed teacher_mode.py sits next to this file, offer its modes (first = default) next to student mode.
    The optional MODES environment variable (e.g. MODES=student) limits or reorders what is offered."""
    global ADDON, MODES, LICENSEE
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "teacher_mode.py")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        lic, why = check_licence(f.read())
    if not lic:
        print(f"teacher_mode.py ignored: {why}", flush=True)
        return
    sys.path.insert(0, here)
    ADDON = importlib.import_module("teacher_mode")
    ADDON.setup(sys.modules[__name__])
    LICENSEE = lic["to"] + (f" (until {lic['expires']})" if lic.get("expires") else "")
    offered = ADDON.MODES + ["student"]
    wanted = [m for m in os.environ.get("MODES", "").split(",") if m in offered]
    MODES = wanted or offered
    print(f"teacher mode licensed to {LICENSEE}: modes " + ", ".join(MODES), flush=True)

if __name__ == "__main__":
    load_addon()
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
