# CV Screener

Upload up to 50 CVs (PDF / DOCX / TXT) and a job spec. An LLM triages every CV into
**GREEN** (qualified), **GRAY** (maybe, verify) or **RED** (fails a hard requirement). It flags
inconsistencies such as experience maths that doesn't add up, contradictions and unexplained gaps,
and it removes duplicate CVs. Results show up as ranked cards plus an Excel sheet for each run.

![Screenshot](docs/screenshot.png)
<!-- screenshot placeholder: add docs/screenshot.png -->

## Setup

Requires Python 3.9+ and any **OpenAI-compatible** LLM endpoint.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit it
python3 app.py              # http://localhost:5153 (or the next free port)
```

`.env`:

| Variable       | Example                                                    |
|----------------|------------------------------------------------------------|
| `LLM_BASE_URL` | `https://api.openai.com/v1` · `http://localhost:11434/v1` (Ollama) · LiteLLM proxy URL |
| `LLM_API_KEY`  | your key (for Ollama, any non-empty string)                |
| `LLM_MODEL`    | default `gpt-4o-mini`                                      |

If these aren't set, the app still starts, prints a clear config error and shows a banner in the UI.
Screening is refused with the same message until you configure it.

## Deploy (Render)

1. Push this folder to a GitHub repo.
2. On [render.com](https://render.com): **New → Web Service**, then pick the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT`
3. Add environment variables: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, plus optional protection:

| Variable | Effect |
|---|---|
| `APP_PASSWORD` | Site asks for this password (any username) |
| `MAX_CVS_PER_DAY` | Caps LLM spend, e.g. `100` |
| `RUN_RETENTION_HOURS` | Deletes uploaded CVs after N hours, e.g. `24` |

Use a single worker: job state is kept in memory.

## How it works

1. **Extract** (`screener/extract.py`): pdfminer.six / python-docx / plain text. A PDF with fewer than
   200 extractable characters counts as scanned and is marked **PROCESSING FAILED**. It is never scored.
2. **Screen** (`screener/screen.py`): one LLM call per CV, run sequentially. The prompt asks for strict JSON.
   The parser is tolerant: it strips ``` fences and `<think>` blocks, then takes the outermost `{…}`.
   If parsing fails, the error is sent back to the model so it can correct itself (3 attempts in total).
   It falls back to `max_completion_tokens` / no `temperature` when a provider rejects those parameters.
   It also enforces the verdict rule: any failed must-have → RED, any unclear one → at most GRAY.
   **Date maths is done in code, not by the LLM** (`screener/timeline.py`): the model only extracts each
   job's start/end dates, the stated "N years experience" and the graduation year. Python then adds up the
   real experience, finds gaps over 12 months and flags claims that don't fit (shown as `[auto-check]`).
   Small models are unreliable at this arithmetic.
3. **Dedupe** (`screener/dedupe.py`): normalized email match first, then fuzzy name match (difflib > 0.9).
4. **Agent** (`screener/agent.py`, optional "Run agent" toggle): for each unique candidate the LLM picks
   actions via OpenAI function calling: `draft_outreach`, `generate_followup_questions`,
   `write_rejection_note` or `flag_for_human_review`. It's capped at 3 tool calls per candidate, and code
   blocks contradictory combinations (e.g. a rejection plus outreach). Writing tools make a separate
   CV-grounded call that is told not to repeat unverified claims. Every decision and its reason is logged to the
   console and `runs/<ts>/agent_log.jsonl`.
5. **Export** (`screener/sheet.py`): `screening.xlsx`, sorted GREEN → GRAY → RED → FAILED with coloured
   rows. Columns I–J hold "Agent action" and "Output"; K–Q are left blank for recruiter notes.

Each run is saved to `runs/<timestamp>/` as `uploads/`, `text/`, `results/NNN.json` (+ `NNN.agent.json`), `results.json`, `agent_log.jsonl` and `screening.xlsx`.

## API

| Method | Path | Body / result |
|---|---|---|
| POST | `/api/upload` | multipart `files[]` + `job_id` → extraction status per file (max 50) |
| POST | `/api/screen` | `{job_id, job_description, must_haves, nice_to_haves, red_flags, run_agent}` (multiline strings, bool) |
| GET  | `/api/status/<job_id>` | `{status, progress, total, files, sheet, email_result}` |
| GET  | `/api/sheet/<job_id>` | download the Excel file |

## Testing

```bash
python tests/make_test_cvs.py          # 5 DOCX CVs: green, gray, red, contradiction, duplicate
python tests/e2e.py http://localhost:5153   # drives the real API against your configured LLM
```

LLM verdicts are judgments, not ground truth. Treat GRAY/RED as a prompt for human review,
not a rejection.
