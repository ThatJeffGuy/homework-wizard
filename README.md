# Homework Wizard 🧙

**It checks your homework. It doesn't do it for you.**

Homework Wizard is made for schools to run for their students. A student pastes their own draft (an essay, a report,
a science write-up). A **local** language model checks every claim against web sources and gives them a report:
what holds up, what's wrong (with the more accurate version), and tips on what would make the piece stronger, each
pointing to the source they should read. It never hands students sentences to paste in; the writing stays theirs.

Everything runs on the school's own machine. The only things that leave it are the web searches. No accounts for
students, no paid API, no student data sent to an AI company.

## What students get
- **Fact check per section:** every checkable claim (numbers, dates, names, science) labelled
  **supported / partly / unclear / contradicted**, with a short reason, a correction where needed and source links.
- **Tips:** 1 to 3 per section, placed under the paragraph they're about. For example: "this needs evidence", "explain why",
  "compare it with...". Each tip points to a source.
- **Topics you could add:** ideas the draft mentions but never explores, with questions to answer and sources to read.
- **Live progress and queue position.** Jobs run one at a time; students can close the page and come back.
- **Hints (up to 3):** on a marked paper, a student highlights part of their own writing and taps **Get a hint**. They
  get a nudge (a guiding question, what to double-check, which source to read), never the answer. A paper scoring at
  least the teacher's threshold (80% by default) earns another hint. New students start with 1 hint (adjustable).
- **Cheat Sheet drawer (the legal kind):** their hints, plus tools that run in the browser and send nothing anywhere:
  draft check (words, reading time, reading level, long sentences, overused words), an APA 7 / MLA 9 citation maker,
  a "before you hand it in" checklist, a unit converter and a 25/5 study timer.
- **Privacy between students:** each browser only lists its own jobs, and every job has an unguessable link.
  A student can have one job waiting at a time.

## Teacher mode
Anyone who isn't logged in uses student mode. Teachers log in with their email and password (the **Staff room**
drawer on the right) and get the teacher tools: classes, assignments, handed-in work, the AI-writing scan and Author mode.
Students never see any of it. The Staff room also lists the teacher's work and holds the hint
settings (score needed to earn a hint, hints a new student starts with).

The school adds teachers on the server:
```
python3 add_teacher.py                  # asks for email, password and confirm password (also changes a password)
python3 add_teacher.py --list
python3 add_teacher.py --remove ms.smith@school.example   # also logs her out everywhere
```
Run it with the same `DATA_DIR` (or `TEACHERS_FILE`) as the server. Passwords are stored salted and hashed
(PBKDF2-SHA256, 600,000 rounds) in `teachers.json`, logins last 30 days, and after 3 wrong passwords an address is
locked out for 1 hour. Behind a reverse proxy, set `CLIENT_IP_HEADER` (see below) or every visitor shares the proxy's
address and one student's wrong guesses would lock everyone out.

## Classes (a familiar, Google Classroom-style workspace)
Students don't need accounts: they **join a class** on the home page with the teacher's **class word** (5 to 7
letters, like `OTTER`) and their name, and that browser remembers it.

- **Home:** class cards. Teachers see students and papers waiting to be graded, and can create a class. Students see
  their classes and a **To-do** list across all of them: Assigned, Missing (past due) and Done.
- **Stream:** teacher announcements; new assignments are posted there automatically.
- **Classwork:** assignments grouped by topic, with instructions, points, a due date and links. Students see their
  status on each (Assigned, Turned in, Graded 8/10, Missing); teachers see turned in / assigned / graded counts.
- **People:** the teacher and the students who joined; teachers can remove a student.
- **Grades** (teachers): a gradebook of students by assignments, with missing work, papers still to grade, and
  averages per student and per assignment.

**The flow:** students practise privately as often as they like (teachers see none of it). When a marked draft is
ready, they **turn it in** from the assignment (or from the paper itself), confirm, and it's locked with a
**Turned in** stamp (**Late** after the due date); the same student can't turn in twice for one assignment. The
teacher opens it, sees a **suggested grade** from the marking score, sets the grade, adds a **private comment** and
**returns** it; the student sees the grade and comment. **Return for a redo** unlocks it so they can turn in again.
Hints are earned when work is turned in with a good score, not for practice drafts.

## Teacher tools
- **Student profiles:** every turned-in paper with grade, marking score, words, reading level and AI-scan result,
  plus missing work.
- **What to do with a paper (teacher form):** mark it, mark it and scan for AI writing, or AI scan only (fast, no web
  research), optionally turned straight in for a student. Any paper a teacher opens also has a
  **Scan for AI writing** button.
- **AI-writing scan:** signals, not a verdict. It looks at sentence rhythm, stock AI phrases, personal voice,
  paragraph sizes, the local model's read (with quoted passages), and, the most useful one, how much the paper differs
  from the same student's earlier papers in that class. Result: few / some / strong signs, each reason shown.
  **No AI detector is reliable** (they often wrongly flag English learners and formal writing), so use it to start a
  conversation, never as proof.

## Author mode (logged-in teachers)
For your own writing: it fact-checks your article and drafts sourced additions and new sections in your voice as
tick-box suggestions, then **Write out the clean copy** assembles the draft, the ticked parts and a source list.

## Uploads and photos
The hand-in form takes a Word file (.docx), a PDF, a text file, or a photo or scan of handwritten work. Text from
Word, text and PDF files goes straight into the box to check before handing in. Photos and scanned PDFs are read by a
local vision model as the first step of marking, and the report notes that it was read from a photo. PDFs need
`pdftotext`/`pdftoppm` (poppler-utils); Pillow (`python3-pil`) is optional and makes big phone photos much faster to read.

Two reading engines (`OCR_ENGINE`):
- **`ollama`** (default): any Ollama vision model (`OCR_MODEL`, default `qwen3-vl:4b-instruct`). Nothing extra to
  set up; about a minute a page on a CPU.
- **`unlimited`** (faster, recommended): [Unlimited-OCR-2](https://huggingface.co/baidu/Unlimited-OCR-2) on a
  llama.cpp server. In our CPU tests it read handwriting 1.5-3x faster than `qwen3-vl:4b` with about the same accuracy,
  and loads in seconds. It also reports how sure it is of every word, so pages where many words were hard to read get a
  **Check the reading** card listing those words, and a *check reading* badge in the teacher's tables. The share of
  unsure words that triggers it is a teacher setting (default 10%); a page where the reader got stuck repeating itself
  is always flagged and the repeat is cut off. Pages are read one at a time (sending several pages in one request made
  it loop in our tests).

  Ollama and mainline llama.cpp can't load Unlimited-OCR-2 yet. Until they can, build llama.cpp with the community patch
  from [cicaba/Unlimited-OCR-2-GGUF](https://huggingface.co/cicaba/Unlimited-OCR-2-GGUF) (read the patch first; it
  targets llama.cpp commit `3c809f443`):
  ```
  git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp && git checkout 3c809f443
  git apply unlimited-ocr2.patch     # llama.cpp-patch/unlimited-ocr2.patch from that repo
  cmake -B build -DBUILD_SHARED_LIBS=OFF && cmake --build build -j --target llama-server
  ./build/bin/llama-server -m Unlimited-OCR-2-Q4_K_M.gguf --mmproj mmproj-Unlimited-OCR-2-F16.gguf \
      --jinja -np 1 -c 16384 -t 12 --temp 0 --host 127.0.0.1 --port 8092
  ```
  then start Homework Wizard with `OCR_ENGINE=unlimited`. The server needs about 7 GB of RAM.

## Requirements
- Python 3.12+ (standard library only, nothing to `pip install`)
- [Ollama](https://ollama.com) with a model that follows instructions well. Tested with `glm-4.7-flash:q4_K_M`
  (30B mixture-of-experts, ~19 GB) on **CPU only**: about 12 tokens/s on an older dual-Xeon server, roughly
  20-60 minutes per piece. For a whole class, use a GPU with 24 GB+ or a smaller model (e.g. `qwen3:8b`, faster but
  makes more mistakes), because jobs queue one after another.
- [SearXNG](https://docs.searxng.org) with the JSON format enabled (`search: formats: [html, json]` in `settings.yml`).

## Run
```
git clone https://github.com/ThatJeffGuy/homework-wizard
cd homework-wizard
ollama pull glm-4.7-flash:q4_K_M
ollama pull qwen3-vl:4b-instruct          # only needed for photos and scans
DATA_DIR=./data/jobs python3 homework_wizard.py     # then open http://localhost:8501
DATA_DIR=./data/jobs python3 add_teacher.py
```

Settings (environment variables):

| Variable | Default | |
|---|---|---|
| `LLM_URL` | `http://127.0.0.1:11436/api/chat` | Ollama chat endpoint (the default port is for a separate CPU-only Ollama; use `11434` for a normal install) |
| `LLM_MODEL` | `glm-4.7-flash:q4_K_M` | any Ollama model |
| `SEARX_URL` | `http://127.0.0.1:8080/search` | SearXNG search endpoint |
| `DATA_DIR` | `/var/lib/homework-wizard/jobs` | where jobs are saved |
| `TEACHERS_FILE` | `teachers.json` next to `DATA_DIR` | teacher logins (hashed passwords) |
| `SECRET_FILE` | `secret.key` next to `DATA_DIR` | signs teacher logins (created automatically) |
| `CLIENT_IP_HEADER` | none | header holding the visitor's real address, set by your proxy, e.g. `CF-Connecting-IP` (Cloudflare) or `X-Real-IP` (nginx with `proxy_set_header X-Real-IP $remote_addr`). Only set it if every request goes through that proxy, since visitors could fake it otherwise |
| `OCR_ENGINE` | `ollama` | `ollama` or `unlimited` (see Uploads and photos) |
| `OCR_MODEL` | `qwen3-vl:4b-instruct` | Ollama vision model that reads photos and scans (`ollama` engine) |
| `OCR_URL` | `LLM_URL`, or `http://127.0.0.1:8092/v1/chat/completions` for `unlimited` | where the reading engine listens |
| `OCR_UNSURE` | `0.75` | `unlimited` engine: a word read with less confidence than this counts as unsure |
| `PORT` | `8501` | web port |
| `THREADS` | Ollama's choice | CPU threads for the model (on CPU-only setups, roughly your physical core count) |

`homework-wizard.service` is an example systemd unit, and `ollama-cpu.service` an example of a second, CPU-only Ollama
instance, handy if your GPU is busy with something else.

**Security:** students need no login. Serve it over HTTPS behind your school's reverse proxy (teacher logins use a
cookie), and keep it on the school network or behind your usual single sign-on if it faces the internet.

## Honest limits
- A local model can still call a claim "supported" based on a weak source. Treat the report as a checklist with links,
  not the final word, and read the sources.
- Sources that repeat the draft itself (say, a copy already published online) are skipped, since a text can't confirm itself.
- Search quality decides fact-check quality. Very new or very niche topics come back "unclear".
- Teacher logins protect the school's server. The code is open source, so anyone running their own copy can add
  their own teacher login; it's built for schools, but people are free to use it however they like.

## Licence
MIT, © 2026 [That Scottish Guy](https://www.everydaysciencestuff.com). Keep the copyright notice (credit) when you reuse it.
