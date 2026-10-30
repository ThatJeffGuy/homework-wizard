# Homework Wizard 🧙

**It checks your homework. It doesn't do it for you.**

Paste your own draft (an essay, a report, a science write-up). A **local** language model checks every claim against
web sources and gives you a report: what holds up, what's wrong (with the more accurate version), and tips on what
would make the piece stronger, each pointing to the source you should read. It never hands you sentences to paste in;
the writing stays yours.

Everything runs on your own machine. The only things that leave it are the web searches. No account, no paid API.

## What you get
- **Fact check per section:** every checkable claim (numbers, dates, names, science) labelled
  **supported / partly / unclear / contradicted**, with a short reason, a correction where needed and links to the sources.
- **Tips:** 1 to 3 per section, placed under the paragraph they're about. For example: "this needs evidence", "explain why",
  "compare it with...". Each tip points to a source.
- **Topics you could add:** ideas your draft mentions but never explores, with questions to answer and sources to read.
- **Live progress** in the browser. You can close the page while it works; every step is saved, so a restart picks up
  where it stopped.

## Teacher mode (optional, licensed add-on, not in this repository)
Teachers, and writers checking their own work before publishing, can ask the author for a licensed `teacher_mode.py`.
Put it in the cloned folder and restart: a mode picker appears, and the page shows who it is licensed to. Teacher mode
also drafts sourced additions and new sections in the writer's voice as tick-box suggestions, and assembles a final text
with a source list. It is kept out of the public code on purpose, so the public project stays a checker and not a
ghost-writer.

Each licence is signed with the author's private key (RSA-3072) and checked against the public key in
`homework_wizard.py`. A missing, empty, edited, forged or expired file is ignored. (`teacher_mode.py` is in `.gitignore`.)

## Requirements
- Python 3.10+ (standard library only, nothing to `pip install`)
- [Ollama](https://ollama.com) with a model that follows instructions well. Tested with `glm-4.7-flash:q4_K_M`
  (30B mixture-of-experts, ~19 GB) on **CPU only**: about 12 tokens/s on an older dual-Xeon server, roughly
  20-60 minutes per piece. Smaller models (e.g. `qwen3:8b`) work and are faster but make more mistakes.
- [SearXNG](https://docs.searxng.org) with the JSON format enabled (`search: formats: [html, json]` in `settings.yml`).

## Run
```
git clone https://github.com/ThatJeffGuy/homework-wizard
cd homework-wizard
ollama pull glm-4.7-flash:q4_K_M
DATA_DIR=./jobs python3 homework_wizard.py     # then open http://localhost:8501
```

Settings (environment variables):

| Variable | Default | |
|---|---|---|
| `LLM_URL` | `http://127.0.0.1:11436/api/chat` | Ollama chat endpoint (the default port is for a separate CPU-only Ollama; use `11434` for a normal install) |
| `LLM_MODEL` | `glm-4.7-flash:q4_K_M` | any Ollama model |
| `SEARX_URL` | `http://127.0.0.1:8080/search` | SearXNG search endpoint |
| `MODES` | all available | only with teacher mode installed: e.g. `student` to hide teacher mode, or `student,teacher` to make student the default |
| `DATA_DIR` | `/var/lib/homework-wizard/jobs` | where jobs are saved |
| `PORT` | `8501` | web port |
| `THREADS` | Ollama's choice | CPU threads for the model (on CPU-only setups, roughly your physical core count) |

`homework-wizard.service` is an example systemd unit, and `ollama-cpu.service` an example of a second, CPU-only Ollama
instance, handy if your GPU is busy with something else.

**Security:** there is no login. Keep it on your LAN or put it behind an authenticating proxy.

## Honest limits
- A local model can still call a claim "supported" based on a weak source. Treat the report as a checklist with links,
  not the final word, and read the sources.
- Sources that repeat your own draft (say, a copy you already published) are skipped, since a text can't confirm itself.
- Search quality decides fact-check quality. Very new or very niche topics come back "unclear".

## Licence
MIT, © 2026 [That Scottish Guy](https://www.everydaysciencestuff.com). Keep the copyright notice (credit) when you reuse it.
