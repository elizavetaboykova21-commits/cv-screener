"""Deterministic date math on the employment history the LLM extracted.

LLMs read dates well but add them up badly, so the model only extracts
start/end dates and this module does the arithmetic.
"""
import re
from datetime import date

GAP_MONTHS = 12
TOLERANCE_YEARS = 1.5


def _month_index(value, today):
    """'2021-03' / '2021' / 'present' -> absolute month number, or None."""
    s = str(value or "").strip().lower()
    if not s:
        return None
    if s in ("present", "current", "now", "today", "ongoing"):
        return today.year * 12 + today.month - 1
    m = re.match(r"^(\d{4})(?:[-/.](\d{1,2}))?", s)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2) or 1)
    if not (1950 <= year <= today.year + 1 and 1 <= month <= 12):
        return None
    return year * 12 + month - 1


def _fmt(idx):
    return "%04d-%02d" % (idx // 12, idx % 12 + 1)


def analyse(history, today=None):
    """Return {'years': float|None, 'gaps': [(start, end, months)], 'roles': n}."""
    today = today or date.today()
    spans = []
    for role in history or []:
        if not isinstance(role, dict):
            continue
        a, b = _month_index(role.get("start"), today), _month_index(role.get("end"), today)
        if a is None or b is None or b < a:
            continue
        spans.append((a, b + 1))  # end month inclusive
    if not spans:
        return {"years": None, "gaps": [], "roles": 0}
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    months = sum(b - a for a, b in merged)
    gaps = []
    for (_, prev_end), (nxt_start, _) in zip(merged, merged[1:]):
        gap = nxt_start - prev_end
        if gap > GAP_MONTHS:
            gaps.append((_fmt(prev_end - 1), _fmt(nxt_start), gap))
    return {"years": round(months / 12, 1), "gaps": gaps, "roles": len(spans)}


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def check(obj, must_haves, today=None):
    """Compute timeline facts and cross-check the model.

    Returns (years, extra_question_marks, must_have_overrides) where overrides maps
    must-have index -> evidence note for N+ years requirements the math disputes.
    """
    today = today or date.today()
    t = analyse(obj.get("employment_history"), today)
    years, notes = t["years"], []
    claimed = _num(obj.get("claimed_years_experience"))
    grad = _num(obj.get("graduation_year"))

    if claimed is not None and years is not None and claimed - years > TOLERANCE_YEARS:
        notes.append("[auto-check] CV claims %g years of experience, but the dated roles add up to "
                     "only ~%g years" % (claimed, years))
    if claimed is not None and grad and 1950 < grad <= today.year:
        since = today.year - int(grad)
        if claimed - since > TOLERANCE_YEARS:
            notes.append("[auto-check] CV claims %g years of experience but graduated in %d "
                         "(~%d years ago)" % (claimed, grad, since))
    for start, end, months in t["gaps"]:
        notes.append("[auto-check] %d-month gap in employment between %s and %s"
                     % (months, start, end))

    overrides = {}
    if years is not None:
        for i, m in enumerate(must_haves):
            req = m.get("requirement", "")
            hit = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)", req, re.I)
            if not hit:
                continue
            need = float(hit.group(1))
            computed_ok = years >= need
            # Only override when the model contradicts the computed math.
            if m.get("met") is (not computed_ok):
                overrides[i] = ("[auto-check] dated roles add up to ~%g years vs %g required; "
                                "model said %s - verify" % (years, need, "met" if m["met"] else "not met"))
    return years, notes, overrides
