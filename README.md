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
- **Privacy between students:** each browser only lists its own jobs, and every job has an unguessable link.
  A student can have one job waiting at a time.

## Teacher mode
Teachers sign in with a personal **teacher key** (a box at the bottom of the page). Teacher mode also drafts sourced
additions and new sections in the writer's voice as tick-box suggestions, and **Build final text** assembles the draft,
the ticked parts and a source list. Handy for model answers, worksheets and the teacher's own writing.
Teacher-mode jobs are only visible to signed-in teachers; students never see the option.

The school creates the keys on the server:
```
python3 homework_wizard.py teacher-key add "Ms Smith"      # prints a new key once; give it to her privately
python3 homework_wizard.py teacher-key list
python3 homework_wizard.py teacher-key remove "Ms Smith"   # also signs her out everywhere
```
Run these with the same `DATA_DIR` (or `KEYS_FILE`) as the server. Only a hash of each key is stored
(`teachers.json`), and sign-ins last 30 days. Keys are 120-bit random and wrong guesses are slowed down.

## Requirements
- Python 3.10+ (standard library only, nothing to `pip install`)
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
DATA_DIR=./data/jobs python3 homework_wizard.py     # then open http://localhost:8501
DATA_DIR=./data/jobs python3 homework_wizard.py teacher-key add "Your Name"
```

Settings (environment variables):

| Variable | Default | |
|---|---|---|
| `LLM_URL` | `http://127.0.0.1:11436/api/chat` | Ollama chat endpoint (the default port is for a separate CPU-only Ollama; use `11434` for a normal install) |
| `LLM_MODEL` | `glm-4.7-flash:q4_K_M` | any Ollama model |
| `SEARX_URL` | `http://127.0.0.1:8080/search` | SearXNG search endpoint |
| `DATA_DIR` | `/var/lib/homework-wizard/jobs` | where jobs are saved |
| `KEYS_FILE` | `teachers.json` next to `DATA_DIR` | hashed teacher keys |
| `SECRET_FILE` | `secret.key` next to `DATA_DIR` | signs teacher sign-ins (created automatically) |
| `PORT` | `8501` | web port |
| `THREADS` | Ollama's choice | CPU threads for the model (on CPU-only setups, roughly your physical core count) |

`homework-wizard.service` is an example systemd unit, and `ollama-cpu.service` an example of a second, CPU-only Ollama
instance, handy if your GPU is busy with something else.

**Security:** students need no login. Serve it over HTTPS behind your school's reverse proxy (teacher sign-ins use a
cookie), and keep it on the school network or behind your usual single sign-on if it faces the internet.

## Honest limits
- A local model can still call a claim "supported" based on a weak source. Treat the report as a checklist with links,
  not the final word, and read the sources.
- Sources that repeat the draft itself (say, a copy already published online) are skipped, since a text can't confirm itself.
- Search quality decides fact-check quality. Very new or very niche topics come back "unclear".
- The code is open source: someone running their own copy can make their own teacher key. Teacher keys protect the
  school's server, where students can't get teacher mode.

## Licence
MIT, © 2026 [That Scottish Guy](https://www.everydaysciencestuff.com). Keep the copyright notice (credit) when you reuse it.
