"""Duplicate-CV detection: normalized email first, fuzzy name fallback."""
import difflib
import re

NAME_THRESHOLD = 0.9


def norm_email(email):
    return (email or "").strip().lower()


def norm_name(name):
    return re.sub(r"[^a-z]", "", (name or "").lower())


def find_duplicates(files):
    """Mark later duplicates in-place.

    `files` is the ordered list of file dicts; each screened one has `result`.
    A duplicate gets `deduped=True` and `duplicate_of=<index of first copy>`.
    Returns the number of duplicates found.
    """
    seen = []  # (index, email, name)
    count = 0
    for f in files:
        r = f.get("result")
        if not r or r.get("verdict") == "PROCESSING FAILED":
            continue
        email, name = norm_email(r.get("email")), norm_name(r.get("name"))
        match = None
        for idx, e, n in seen:
            if email and e and email == e:
                match = idx
                break
            if name and n and difflib.SequenceMatcher(None, name, n).ratio() > NAME_THRESHOLD:
                match = idx
                break
        if match is None:
            seen.append((f["index"], email, name))
            f["deduped"] = False
        else:
            f["deduped"] = True
            f["duplicate_of"] = match
            count += 1
    return count
