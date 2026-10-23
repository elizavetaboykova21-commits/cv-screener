"""One LLM call per CV, strict-JSON output with tolerant parsing and self-correcting retries."""
import json
import re
import time
from datetime import date

import requests

import settings
from screener import timeline

VERDICTS = ("GREEN", "GRAY", "RED", "PROCESSING FAILED")
MAX_ATTEMPTS = 3
MAX_CV_CHARS = 30000

# Provider quirks learned at runtime, so we only pay for a rejected request once.
_QUIRKS = {"token_param": "max_tokens", "no_temperature": False}

SYSTEM_PROMPT = (
    "You are a meticulous technical recruiter screening a CV against a job spec. "
    "You respond with STRICT JSON ONLY: a single JSON object, no markdown, no code fences, "
    "no commentary before or after. Your entire visible response must be parseable by json.loads. "
    "Never invent facts that are not in the CV; if something is not stated, say so."
)

JSON_SHAPE = """{
  "verdict": "GREEN" | "GRAY" | "RED",
  "name": "", "email": "", "phone": "", "current_title": "", "current_company": "",
  "claimed_years_experience": null,
  "graduation_year": null,
  "employment_history": [{"title": "", "company": "", "start": "YYYY-MM", "end": "YYYY-MM or present"}],
  "why_fit": "2-3 sentences on why this candidate fits the JD",
  "why_not_fit": "concrete gaps vs must-haves (empty string if none)",
  "question_marks": ["each inconsistency/gap/thing to verify in an interview - quote the CV's own words/dates when possible"],
  "must_have_results": [{"requirement": "", "met": true, "evidence": ""}]
}"""


def _bullets(items):
    return "\n".join("- " + i for i in items) if items else "(none)"


def build_user_prompt(spec, cv_text):
    cv_text = cv_text.strip()
    if len(cv_text) > MAX_CV_CHARS:
        cv_text = cv_text[:MAX_CV_CHARS] + "\n[... CV truncated ...]"
    today = date.today().strftime("%B %Y")
    return f"""Today's date: {today} (use it for any role that runs "to present").

=== JOB SPEC ===
Description:
{spec.get("job_description") or "(none)"}

Must-haves (each is PASS/FAIL):
{_bullets(spec.get("must_haves"))}

Nice-to-haves:
{_bullets(spec.get("nice_to_haves"))}

Red flags:
{_bullets(spec.get("red_flags"))}

=== CV TEXT ===
{cv_text}
=== END CV ===

=== INSTRUCTIONS ===
1. Judge ONLY against the job spec above. Do not reward things the spec does not ask for.
2. Evaluate EVERY must-have as pass/fail and list each one in "must_have_results" with
   "met": true (clear evidence), false (clearly missing/contradicted) or null (unclear), plus
   the evidence quoted or paraphrased from the CV.
3. Verdict rules:
   - If ANY must-have is not met (false) -> "RED".
   - If all must-haves pass but some evidence is weak or unclear (null), or red flags are present -> "GRAY".
   - All must-haves pass with confident evidence -> "GREEN".
4. Look hard for inconsistencies and put each one in "question_marks":
   - Compare any stated total years of experience against the dated employment history and the education
     dates (e.g. "graduated 2020" cannot support "12 years experience").
   - Contradictions between dates, titles, seniority claims and education.
   - Unexplained gaps longer than 12 months between roles (state the dates).
   - Anything matching the red-flags list.
   Quote the CV's own words and dates where possible. Use an empty list if there is nothing.
5. Extraction fields (copy facts, do not calculate):
   - "employment_history": EVERY dated role in the CV, start/end as "YYYY-MM" (or "YYYY" if only the year
     is given, "present" for current roles). Skip career breaks.
   - "claimed_years_experience": the total years of experience the CV itself STATES (e.g. "12 years experience"
     -> 12), or null if it never states a number.
   - "graduation_year": year of the most recent degree, or null.
6. Use empty strings for contact fields not present in the CV. Do not guess emails or phones.

Return exactly this JSON shape and nothing else:
{JSON_SHAPE}"""


class LLMError(Exception):
    pass


def extract_json(text):
    """Tolerantly pull a JSON object out of a model response."""
    if text is None or not text.strip():
        raise ValueError(
            "empty response (a reasoning model may have spent all its tokens thinking)"
        )
    t = text.strip()
    # Drop <think>...</think> blocks some local reasoning models emit inline.
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S | re.I).strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    obj = json.loads(t[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    return obj


def _post(messages):
    """Return the assistant text for a plain chat completion."""
    return chat(messages).get("content")


def chat(messages, **extra):
    """POST to /chat/completions, adapting to provider quirks. Returns the assistant message dict.

    `extra` is merged into the request body (e.g. tools=..., tool_choice=...)."""
    url = settings.LLM_BASE_URL + "/chat/completions"
    headers = {"Authorization": "Bearer " + settings.LLM_API_KEY, "Content-Type": "application/json"}
    body = {"model": settings.LLM_MODEL, "messages": messages,
            _QUIRKS["token_param"]: settings.LLM_MAX_TOKENS, **extra}
    if not _QUIRKS["no_temperature"]:
        body["temperature"] = 0.2
    # Up to two quirk adaptations: max_tokens -> max_completion_tokens, and dropping temperature.
    for _ in range(3):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=settings.LLM_TIMEOUT)
        except requests.RequestException as e:
            raise LLMError("cannot reach LLM at %s (%s)" % (settings.LLM_BASE_URL, type(e).__name__))
        if resp.status_code == 400:
            msg = resp.text.lower()
            if "max_tokens" in msg and "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
                _QUIRKS["token_param"] = "max_completion_tokens"
                continue
            if "temperature" in msg and "temperature" in body:
                body.pop("temperature")
                _QUIRKS["no_temperature"] = True
                continue
        if resp.status_code in (401, 403):
            raise LLMError("LLM auth failed (HTTP %d) - check LLM_API_KEY" % resp.status_code)
        if resp.status_code == 404:
            raise LLMError("LLM endpoint/model not found (HTTP 404) - check LLM_BASE_URL and LLM_MODEL")
        if resp.status_code >= 400:
            raise LLMError("LLM HTTP %d: %s" % (resp.status_code, resp.text[:300]))
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
            if not isinstance(msg, dict):
                raise TypeError
            return msg
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("unexpected LLM response shape: %s" % resp.text[:300])
    raise LLMError("LLM rejected request parameters: %s" % resp.text[:300])


def _as_str(v):
    return "" if v is None else str(v).strip()


def normalize(obj):
    """Coerce the model output into the exact result shape and enforce verdict rules."""
    mh = []
    for item in obj.get("must_have_results") or []:
        if isinstance(item, dict):
            met = item.get("met")
            if isinstance(met, str):
                met = {"true": True, "false": False}.get(met.strip().lower())
            mh.append({
                "requirement": _as_str(item.get("requirement")),
                "met": met if met in (True, False) else None,
                "evidence": _as_str(item.get("evidence")),
            })
    qm = obj.get("question_marks") or []
    if isinstance(qm, str):
        qm = [qm]
    qm = [_as_str(q) for q in qm if _as_str(q)]

    verdict = _as_str(obj.get("verdict")).upper()
    if verdict in ("GREY", "AMBER", "YELLOW", "MAYBE"):
        verdict = "GRAY"
    if verdict not in VERDICTS:
        raise ValueError('"verdict" must be one of GREEN, GRAY, RED (got %r)' % obj.get("verdict"))

    # Code does the date math; where it contradicts the model on an "N+ years" must-have, mark it unclear.
    ye, auto_notes, overrides = timeline.check(obj, mh)
    for i, note in overrides.items():
        mh[i]["met"] = None
        mh[i]["evidence"] = (mh[i]["evidence"] + " " if mh[i]["evidence"] else "") + note
    qm = qm + [n for n in auto_notes if n not in qm]

    # Deterministic guardrail: the verdict must agree with the must-have results.
    adjusted = None
    if verdict in ("GREEN", "GRAY") and any(m["met"] is False for m in mh):
        adjusted, verdict = verdict, "RED"
    elif verdict == "GREEN" and (any(m["met"] is None for m in mh) or auto_notes):
        adjusted, verdict = verdict, "GRAY"


    out = {
        "verdict": verdict,
        "name": _as_str(obj.get("name")),
        "email": _as_str(obj.get("email")),
        "phone": _as_str(obj.get("phone")),
        "current_title": _as_str(obj.get("current_title")),
        "current_company": _as_str(obj.get("current_company")),
        "years_experience": ye,
        "claimed_years_experience": timeline._num(obj.get("claimed_years_experience")),
        "graduation_year": obj.get("graduation_year"),
        "employment_history": [r for r in (obj.get("employment_history") or []) if isinstance(r, dict)],
        "why_fit": _as_str(obj.get("why_fit")),
        "why_not_fit": _as_str(obj.get("why_not_fit")),
        "question_marks": qm,
        "must_have_results": mh,
    }
    if adjusted:
        out["verdict_adjusted_from"] = adjusted
    return out


def failed(reason, filename=""):
    return {
        "verdict": "PROCESSING FAILED",
        "name": filename, "email": "", "phone": "", "current_title": "", "current_company": "",
        "years_experience": None,
        "why_fit": "", "why_not_fit": "",
        "question_marks": [], "must_have_results": [],
        "error": reason,
    }


def screen_cv(cv_text, spec, filename=""):
    """Screen one CV. Never raises: returns a PROCESSING FAILED result on any failure."""
    cfg = settings.config_error()
    if cfg:
        return failed(cfg, filename)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(spec, cv_text)},
    ]
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            content = _post(messages)
        except LLMError as e:
            last_err = str(e)
            # Config/auth problems will not fix themselves; only retry transport-ish errors.
            if "auth failed" in last_err or "not found" in last_err:
                break
            time.sleep(1.5 * attempt)
            continue
        try:
            result = normalize(extract_json(content))
            result["attempts"] = attempt
            return result
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            last_err = "invalid JSON: %s" % e
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({
                "role": "user",
                "content": "Your previous response could not be parsed (%s). Respond again with ONLY "
                "the single JSON object in the required shape - no markdown fences, no text "
                "before or after." % e,
            })
    return failed("LLM failed after %d attempt(s): %s" % (MAX_ATTEMPTS, last_err), filename)
