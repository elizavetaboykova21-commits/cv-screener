"""CV Screener - Flask app. Run: python3 app.py"""
import hmac
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from datetime import date, datetime

from flask import Flask, abort, jsonify, make_response, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

import settings
from screener.agent import run_agent
from screener.dedupe import find_duplicates
from screener.extract import ExtractionError, extract_text
from screener.screen import failed, screen_cv
from screener.sheet import build_sheet, sort_key

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
MAX_FILES = 50
JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

JOBS = {}
LOCK = threading.Lock()
USAGE = {"day": None, "cvs": 0}  # CVs screened today, for MAX_CVS_PER_DAY


@app.before_request
def require_password():
    """Optional HTTP Basic auth for public deployments (any username, password = APP_PASSWORD)."""
    if not settings.APP_PASSWORD:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or "").encode(), settings.APP_PASSWORD.encode()):
        return None
    resp = make_response("Password required", 401)
    resp.headers["WWW-Authenticate"] = 'Basic realm="CV Screener"'
    return resp


def _cleanup_old_runs():
    """Delete run folders (uploaded CVs etc.) older than RUN_RETENTION_HOURS."""
    if settings.RUN_RETENTION_HOURS <= 0 or not os.path.isdir(RUNS):
        return
    cutoff = time.time() - settings.RUN_RETENTION_HOURS * 3600
    live = {j["run_dir"] for j in JOBS.values() if j["status"] != "done"}
    for name in os.listdir(RUNS):
        path = os.path.join(RUNS, name)
        if os.path.isdir(path) and path not in live and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
    for jid in [k for k, j in JOBS.items() if not os.path.isdir(j["run_dir"])]:
        JOBS.pop(jid, None)


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _get_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(make_response(*_err("unknown job_id", 404)))
    return job


def _public_file(f):
    return {k: v for k, v in f.items() if k not in ("text_path",)}


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.get("/api/config")
def config():
    return jsonify({"model": settings.LLM_MODEL, "base_url": settings.LLM_BASE_URL,
                    "error": settings.config_error(), "max_files": MAX_FILES})


@app.post("/api/upload")
def upload():
    job_id = (request.form.get("job_id") or "").strip()
    if not JOB_ID_RE.match(job_id):
        return _err("missing or invalid job_id")
    uploads = request.files.getlist("files")
    if not uploads:
        return _err("no files uploaded (multipart field 'files')")

    with LOCK:
        _cleanup_old_runs()
        job = JOBS.get(job_id)
        if job and job["status"] != "extracted":
            return _err("job already %s; start a new job to upload more" % job["status"], 409)
        already = len(job["files"]) if job else 0
        if already + len(uploads) > MAX_FILES:
            return _err("too many files: max %d per run (%d already uploaded, %d sent)"
                        % (MAX_FILES, already, len(uploads)), 413)
        if not job:
            ts =datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job_id[:6]
            run_dir = os.path.join(RUNS, ts)
            for sub in ("uploads", "text", "results"):
                os.makedirs(os.path.join(run_dir, sub), exist_ok=True)
            job = JOBS[job_id] = {"job_id": job_id, "run_dir": run_dir, "status": "extracted",
                                  "progress": 0, "total": 0, "files": [], "sheet": None,
                                  "email_result": None, "error": None}

    run_dir = job["run_dir"]
    new = []
    for up in uploads:
        index = len(job["files"]) + len(new) + 1
        original = os.path.basename((up.filename or "").replace("\\", "/")) or "file"
        safe = secure_filename(original) or "file"
        path = os.path.join(run_dir, "uploads", "%03d_%s" % (index, safe))
        up.save(path)
        entry = {"index": index, "filename": original, "path": os.path.relpath(path, BASE),
                 "size": os.path.getsize(path), "status": None, "error": None, "result": None}
        try:
            text = extract_text(path)
            tpath = os.path.join(run_dir, "text", "%03d.txt" % index)
            with open(tpath, "w", encoding="utf-8") as fh:
                fh.write(text)
            entry.update(status="✓ extracted", text_path=tpath, chars=len(text))
        except ExtractionError as e:
            entry.update(status="✗ " + str(e), error=str(e),
                         result=failed("PROCESSING FAILED: " + str(e), original))
        except Exception as e:  # never let one bad file kill the upload
            entry.update(status="✗ extraction error", error="%s: %s" % (type(e).__name__, e),
                         result=failed("extraction error: %s" % e, original))
        new.append(entry)

    with LOCK:
        job["files"].extend(new)
        job["total"] = len(job["files"])
    return jsonify({"job_id": job_id, "files": [_public_file(f) for f in new]})


def _lines(v):
    if isinstance(v, list):
        items = v
    else:
        items = (v or "").splitlines()
    return [re.sub(r"^\s*[-*•]\s*", "", s).strip() for s in items if s and s.strip()]


def _run(job):
    run_dir, spec = job["run_dir"], job["spec"]
    try:
        for f in job["files"]:  # sequential, deterministic order
            if f.get("text_path"):
                f["status"] = "⏳ screening"
                with open(f["text_path"], encoding="utf-8") as fh:
                    text = fh.read()
                t0 = time.time()
                f["result"] = screen_cv(text, spec, f["filename"])
                f["result"]["seconds"] = round(time.time() - t0, 1)
                f["status"] = f["result"]["verdict"]
            with open(os.path.join(run_dir, "results", "%03d.json" % f["index"]), "w") as fh:
                json.dump(f["result"], fh, indent=2, ensure_ascii=False)
            job["progress"] += 1

        job["deduped"] = find_duplicates(job["files"])
        by_index = {f["index"]: f for f in job["files"]}
        for f in job["files"]:
            if f.get("deduped"):
                orig = by_index[f["duplicate_of"]]
                f["duplicate_of_name"] = "%s (%s)" % (orig["result"].get("name") or "?", orig["filename"])

        if job.get("run_agent"):
            _run_agent_step(job)

        files = sorted(job["files"], key=sort_key)
        with open(os.path.join(run_dir, "results.json"), "w") as fh:
            json.dump({"job_id": job["job_id"], "spec": spec, "model": settings.LLM_MODEL,
                       "files": [_public_file(f) for f in files]}, fh, indent=2, ensure_ascii=False)
        xlsx = os.path.join(run_dir, "screening.xlsx")
        build_sheet(job["files"], xlsx)
        job["sheet"] = {"path": os.path.relpath(xlsx, BASE), "url": "/api/sheet/" + job["job_id"]}
    except Exception as e:  # surface instead of hanging the UI forever
        job["error"] = "screening run crashed: %s: %s" % (type(e).__name__, e)
        app.logger.exception("screening run crashed")
    finally:
        job["status"] = "done"


def _run_agent_step(job):
    """Agent phase: for each unique, successfully screened candidate, let the LLM pick actions."""
    run_dir, spec = job["run_dir"], job["spec"]
    eligible = [f for f in job["files"] if f.get("text_path") and not f.get("deduped")
                and (f.get("result") or {}).get("verdict") != "PROCESSING FAILED"]
    for f in job["files"]:
        if f not in eligible:
            f["agent"] = {"actions": [], "summary": "", "error": None,
                          "skipped": "duplicate" if f.get("deduped") else "processing failed"}
    job.update(status="agent", agent_progress=0, agent_total=len(eligible))
    log_path = os.path.join(run_dir, "agent_log.jsonl")

    def log(event):
        event = dict(event, file=f["filename"], time=datetime.now().isoformat(timespec="seconds"))
        with LOCK, open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event["event"] == "tool_call":
            print("[agent] %s -> %s | %s" % (event["candidate"], event["tool"], event["reason"]), flush=True)
        elif event["event"] in ("error", "skipped", "invalid_call"):
            print("[agent] %s -> %s %s" % (event["candidate"], event["event"],
                                           event.get("error") or event.get("why") or ""), flush=True)

    for f in eligible:
        verdict = f["result"]["verdict"]
        f["status"] = "🤖 agent"
        with open(f["text_path"], encoding="utf-8") as fh:
            text = fh.read()
        f["agent"] = run_agent(f["result"], text, spec, log=log)
        with open(os.path.join(run_dir, "results", "%03d.agent.json" % f["index"]), "w") as fh:
            json.dump(f["agent"], fh, indent=2, ensure_ascii=False)
        f["status"] = verdict
        job["agent_progress"] += 1


@app.post("/api/screen")
def screen():
    data = request.get_json(silent=True) or {}
    job = _get_job(data.get("job_id", ""))
    cfg = settings.config_error()
    if cfg:
        return _err(cfg, 503)
    if not (data.get("job_description") or "").strip():
        return _err("job_description is required")
    must = _lines(data.get("must_haves"))
    if not must:
        return _err("at least one must-have is required (one per line)")
    with LOCK:
        if job["status"] != "extracted":
            return _err("job is already %s" % job["status"], 409)
        n = sum(1 for f in job["files"] if f.get("text_path"))
        today = date.today().isoformat()
        if USAGE["day"] != today:
            USAGE.update(day=today, cvs=0)
        if settings.MAX_CVS_PER_DAY and USAGE["cvs"] + n > settings.MAX_CVS_PER_DAY:
            left = max(settings.MAX_CVS_PER_DAY - USAGE["cvs"], 0)
            return _err("Daily limit reached: this site screens at most %d CVs per day (%d left today)."
                        % (settings.MAX_CVS_PER_DAY, left), 429)
        USAGE["cvs"] += n
        job["spec"] = {"job_description": data["job_description"].strip(), "must_haves": must,
                       "nice_to_haves": _lines(data.get("nice_to_haves")),
                       "red_flags": _lines(data.get("red_flags"))}
        job["run_agent"] = bool(data.get("run_agent"))
        job["status"] = "screening"
        job["progress"] = 0
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return jsonify({"job_id": job["job_id"], "status": "screening", "total": job["total"]})


@app.get("/api/status/<job_id>")
def status(job_id):
    job = _get_job(job_id)
    return jsonify({"status": job["status"], "progress": job["progress"], "total": job["total"],
                    "files": [_public_file(f) for f in job["files"]], "sheet": job["sheet"],
                    "deduped": job.get("deduped", 0), "error": job.get("error"),
                    "run_agent": job.get("run_agent", False),
                    "agent_progress": job.get("agent_progress", 0), "agent_total": job.get("agent_total", 0),
                    "email_result": job["email_result"]})


@app.get("/api/sheet/<job_id>")
def sheet(job_id):
    job = _get_job(job_id)
    if not job["sheet"]:
        return _err("sheet not ready", 404)
    return send_file(os.path.join(BASE, job["sheet"]["path"]), as_attachment=True,
                     download_name="screening-%s.xlsx" % os.path.basename(job["run_dir"]))


@app.errorhandler(413)
def too_large(_e):
    return _err("upload too large (max 200 MB per request)", 413)


def free_port(start=5153, tries=50):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # match werkzeug; ignore TIME_WAIT
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port found in %d-%d" % (start, start + tries - 1))


if __name__ == "__main__":
    os.makedirs(RUNS, exist_ok=True)
    err = settings.config_error()
    if err:
        bar = "!" * 78
        print("\n%s\n  CONFIG ERROR: %s\n  The UI will load, but screening is disabled until this is fixed.\n%s\n"
              % (bar, err, bar), file=sys.stderr)
    else:
        print("LLM: %s @ %s" % (settings.LLM_MODEL, settings.LLM_BASE_URL))
    port = free_port(int(os.environ.get("PORT", 5153)))
    print("CV Screener running at http://localhost:%d" % port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
