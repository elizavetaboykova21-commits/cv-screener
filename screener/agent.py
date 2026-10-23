"""Post-screening agent: the LLM picks follow-up actions per candidate via function calling.

The model sees the screening result + CV evidence and decides which tools to call
(max MAX_TOOL_CALLS per candidate). Writing tools are real functions that make a
focused, CV-grounded generation call; flag_for_human_review is deterministic.
"""
import json

from screener import screen

MAX_TOOL_CALLS = 3
MAX_TURNS = MAX_TOOL_CALLS + 1  # +1 lets the model say "done" after its last tool
CV_CHARS = 8000

TOOLS = [
    {"type": "function", "function": {
        "name": "draft_outreach",
        "description": "Draft a personalised outreach message inviting a promising candidate to talk. "
                       "Use for candidates worth pursuing.",
        "parameters": {"type": "object", "properties": {
            "candidate_name": {"type": "string"},
            "reasons": {"type": "array", "items": {"type": "string"},
                        "description": "Specific CV-backed reasons they fit the role."},
        }, "required": ["candidate_name", "reasons"]}}},
    {"type": "function", "function": {
        "name": "generate_followup_questions",
        "description": "Generate 2-3 screening questions that resolve unclear or unverified must-haves "
                       "or inconsistencies in the CV.",
        "parameters": {"type": "object", "properties": {
            "candidate_name": {"type": "string"},
            "gaps": {"type": "array", "items": {"type": "string"},
                     "description": "The unclear must-haves / inconsistencies to resolve."},
        }, "required": ["candidate_name", "gaps"]}}},
    {"type": "function", "function": {
        "name": "write_rejection_note",
        "description": "Write a short, polite rejection. Only for candidates who clearly fail a must-have.",
        "parameters": {"type": "object", "properties": {
            "candidate_name": {"type": "string"},
            "reason": {"type": "string", "description": "The internal reason (clearly failed must-have)."},
        }, "required": ["candidate_name", "reason"]}}},
    {"type": "function", "function": {
        "name": "flag_for_human_review",
        "description": "Flag the candidate for a recruiter to look at when the evidence is ambiguous, "
                       "contradictory, or the screening verdict looks questionable.",
        "parameters": {"type": "object", "properties": {
            "candidate_name": {"type": "string"},
            "reason": {"type": "string"},
        }, "required": ["candidate_name", "reason"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
# Actions that contradict each other for the same candidate (enforced in code, not just the prompt).
CONFLICTS = {
    "write_rejection_note": {"draft_outreach", "generate_followup_questions"},
    "draft_outreach": {"write_rejection_note"},
    "generate_followup_questions": {"write_rejection_note"},
}
LABELS = {"draft_outreach": "Outreach", "generate_followup_questions": "Follow-up questions",
          "write_rejection_note": "Rejection note", "flag_for_human_review": "Human review"}

SYSTEM_PROMPT = f"""You are a recruiting agent acting on the result of an automated CV screen.
Decide which follow-up action(s) to take for ONE candidate by calling the tools. You may call at most
{MAX_TOOL_CALLS} tools in total and each tool at most once. Base every decision on the screening result
and the CV evidence you are given - never on assumptions.

Guidance (use judgment, these are not fixed rules):
- Clearly qualified -> draft_outreach.
- Promising but some must-haves are unclear/unverified, or there are gaps/inconsistencies worth asking
  about -> generate_followup_questions (often together with draft_outreach).
- Clearly fails a must-have -> write_rejection_note. Do NOT reject someone whose failure rests on
  unclear or disputed evidence.
- Evidence is ambiguous or contradictory, or the verdict looks wrong given the CV ->
  flag_for_human_review (you may combine it with other actions).
- write_rejection_note cannot be combined with draft_outreach or generate_followup_questions.
- Claims listed under "Things to verify" are UNVERIFIED: never cite them as reasons in draft_outreach.

Before each tool call, write ONE short sentence in your message explaining why you are taking that action.
When you have finished, reply with a one-sentence summary and no tool call."""


def _context(result, cv_text, spec):
    mh = "\n".join("- [%s] %s: %s" % ({True: "MET", False: "NOT MET"}.get(m.get("met"), "UNCLEAR"),
                                      m.get("requirement"), m.get("evidence"))
                   for m in result.get("must_have_results") or []) or "(none)"
    qm = "\n".join("- " + q for q in result.get("question_marks") or []) or "(none)"
    return f"""ROLE: {spec.get("job_description", "")[:1500]}

SCREENING RESULT for {result.get("name") or "the candidate"}:
Verdict: {result.get("verdict")}{" (adjusted from " + result["verdict_adjusted_from"] + ")" if result.get("verdict_adjusted_from") else ""}
Current: {result.get("current_title")} at {result.get("current_company")}
Years of experience (computed from dated roles): {result.get("years_experience")}
Why fit: {result.get("why_fit")}
Why not fit: {result.get("why_not_fit") or "(nothing)"}
Must-haves:
{mh}
Things to verify:
{qm}

CV TEXT:
{cv_text.strip()[:CV_CHARS]}"""


def _as_text(v):
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if str(x).strip())
    return str(v or "").strip()


WRITER = {
    "draft_outreach": (
        "Write a personalised recruiter outreach message (90-140 words) to {name}, inviting them to a short call "
        "about the role. Reference 2 specific, real details from their CV. Why they fit: {arg}. "
        "Plain text, no subject line, no placeholders like [Company]; say 'our team' and sign off as 'The Hiring Team'."),
    "generate_followup_questions": (
        "Write 2-3 numbered screening questions for {name} that would resolve these open points: {arg}. "
        "Each question must cite the CV's own facts or dates and be answerable in a short call. "
        "Plain text, numbered list only."),
    "write_rejection_note": (
        "Write a short, polite rejection message (50-80 words) to {name} for the role. Internal reason: {arg}. "
        "Be kind and specific in at most one sentence about fit, make no false promises, no placeholders, "
        "sign off as 'The Hiring Team'. Plain text."),
}


def _execute(name, args, cv_text, spec, result):
    who = str(args.get("candidate_name") or "the candidate").strip()
    if name == "flag_for_human_review":
        return "Flagged for recruiter review: " + (_as_text(args.get("reason")) or "no reason given")
    arg = _as_text(args.get("reasons") or args.get("gaps") or args.get("reason"))
    prompt = WRITER[name].format(name=who, arg=arg or "(not specified)")
    unverified = result.get("question_marks") or []
    if unverified:
        prompt += ("\nThese points are UNVERIFIED or inconsistent - do NOT state, repeat or praise any of them "
                   "as fact (you may ask about them only if the task is to write questions): "
                   + "; ".join(unverified))
    messages = [
        {"role": "system", "content": "You write concise, professional recruiting messages grounded only in "
                                      "the CV provided. Output only the message text."},
        {"role": "user", "content": "ROLE: %s\n\nCV:\n%s\n\nTASK: %s"
                                    % (spec.get("job_description", "")[:1500], cv_text.strip()[:CV_CHARS], prompt)},
    ]
    text = (screen._post(messages) or "").strip()
    if not text:
        raise screen.LLMError("writer returned an empty message")
    return text


def run_agent(result, cv_text, spec, log=lambda event: None):
    """Run the agent loop for one screened candidate. Never raises.

    Returns {"actions": [...], "summary": str, "error": str|None}; each action is
    {"step", "tool", "label", "args", "reason", "output"}. `log` receives one dict per decision.
    """
    name = result.get("name") or "candidate"
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _context(result, cv_text, spec)}]
    actions, summary, error, used, called = [], "", None, 0, set()
    try:
        for _ in range(MAX_TURNS):
            if used >= MAX_TOOL_CALLS:
                break
            msg = screen.chat(messages, tools=TOOLS, tool_choice="required" if used == 0 else "auto")
            thought = (msg.get("content") or "").strip()
            calls = msg.get("tool_calls") or []
            turn = {"role": "assistant", "content": msg.get("content")}
            if calls:
                turn["tool_calls"] = calls
            messages.append(turn)
            if not calls:
                summary = thought
                log({"candidate": name, "event": "done", "summary": thought})
                break
            for tc in calls:
                fn = (tc.get("function") or {})
                tool = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = None
                if used >= MAX_TOOL_CALLS:
                    out = "Not executed: limit of %d tool calls per candidate reached." % MAX_TOOL_CALLS
                    log({"candidate": name, "event": "skipped", "tool": tool, "why": "tool-call limit"})
                elif tool not in TOOL_NAMES or not isinstance(args, dict):
                    out = "Error: unknown tool or invalid JSON arguments."
                    log({"candidate": name, "event": "invalid_call", "tool": tool})
                elif tool in called:
                    out = "Not executed: %s was already called for this candidate." % tool
                    log({"candidate": name, "event": "skipped", "tool": tool, "why": "duplicate call"})
                elif CONFLICTS.get(tool, set()) & called:
                    clash = ", ".join(sorted(CONFLICTS[tool] & called))
                    out = "Not executed: %s conflicts with %s, which was already done." % (tool, clash)
                    log({"candidate": name, "event": "skipped", "tool": tool, "why": "conflicts with " + clash})
                else:
                    used += 1
                    called.add(tool)
                    reason = thought or _as_text(args.get("reasons") or args.get("gaps") or args.get("reason"))
                    out = _execute(tool, args, cv_text, spec, result)
                    actions.append({"step": used, "tool": tool, "label": LABELS[tool], "args": args,
                                    "reason": reason, "output": out})
                    log({"candidate": name, "event": "tool_call", "step": used, "tool": tool,
                         "reason": reason, "args": args})
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
    except screen.LLMError as e:
        error = str(e)
        if "tool" in error.lower() and "HTTP 400" in error:
            error = "the configured model/provider does not support function calling: " + error
        log({"candidate": name, "event": "error", "error": error})
    if not actions and not error:
        error = "agent finished without taking any action"
    return {"actions": actions, "summary": summary, "error": error}
