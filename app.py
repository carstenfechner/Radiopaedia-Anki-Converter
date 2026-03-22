"""
app.py  —  Radiopaedia → Anki web interface

Usage:
    python app.py            # development (port 5000)
    python app.py --port 8080
    gunicorn -w 1 app:app    # production (SINGLE worker required)

Deployable behind nginx — set X-Accel-Buffering: no for SSE support.
"""

import ctypes
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template,
                   request, send_file, stream_with_context)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job registry  (in-memory — requires single worker process)
# ---------------------------------------------------------------------------

jobs: dict[str, dict] = {}
CLEANUP_DELAY_S = 600          # 10 min before temp dir is deleted
_playwright_sem = threading.Semaphore(3)   # max 3 concurrent Chromium instances


# ---------------------------------------------------------------------------
# Sleep prevention (cross-platform)
# ---------------------------------------------------------------------------

def _prevent_sleep():
    """Block OS idle-sleep. Returns an opaque handle for _allow_sleep()."""
    if sys.platform == "darwin":
        try:
            return subprocess.Popen(["caffeinate", "-i"])
        except FileNotFoundError:
            return None
    elif sys.platform == "win32":
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
        return "windows"
    elif sys.platform.startswith("linux"):
        try:
            return subprocess.Popen(
                ["systemd-inhibit", "--what=sleep", "--who=RadiopaediaAnki",
                 "--why=Downloading deck", "--mode=block", "sleep", "infinity"]
            )
        except FileNotFoundError:
            return None
    return None


def _allow_sleep(handle) -> None:
    """Release the sleep-prevention lock acquired by _prevent_sleep()."""
    if handle == "windows":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS only
    elif handle is not None:
        handle.terminate()


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_job(job_id: str, folder_title: str,
             article_urls: list[str], case_urls: list[str],
             max_cases_per_article: int | None = None,
             cases_from_articles: bool = False,
             max_quiz_cases_per_article: int | None = None) -> None:
    """Runs in a daemon thread. Puts SSE events onto the job queue."""
    job = jobs[job_id]
    q   = job["queue"]
    tmp = job["tmp_dir"]

    # Prevent the OS from sleeping while the job runs (macOS / Windows / Linux)
    _wake_handle = _prevent_sleep()

    def cb(msg: dict) -> None:
        q.put(msg)

    try:
        from downloader import run as dl_run, download_article as dl_article
        from anki_builder import build_package

        total = len(article_urls) + len(case_urls)
        done  = 0

        article_data_list: list[dict]       = []
        case_data_list:    list[list[dict]] = []  # each entry = one card (list of sub-cases)

        # ---- Articles ----
        for i, url in enumerate(article_urls, start=1):
            cb({"type": "progress",
                "message": f"Downloading article {i}/{len(article_urls)}: {url}",
                "pct": int(done / total * 88)})
            with _playwright_sem:
                result = dl_article(url=url, output_dir=tmp,
                                    headed=False, delay_ms=500,
                                    progress_cb=cb, webp=True,
                                    max_cases=max_cases_per_article)
            article_data_list.append(result)
            if cases_from_articles:
                groups = result.get("linked_case_groups", [])
                if max_quiz_cases_per_article is not None:
                    groups = groups[:max_quiz_cases_per_article]
                for group in groups:
                    if group:
                        case_data_list.append(group)
            cb({"type": "item_done",
                "message": f"Article {result['rid']} \u2014 {result['plain_title']}"})
            done += 1

        # ---- Cases ----
        for i, url in enumerate(case_urls, start=1):
            cb({"type": "progress",
                "message": f"Downloading case {i}/{len(case_urls)}: {url}",
                "pct": int(done / total * 88)})
            with _playwright_sem:
                results = dl_run(case_url=url, output_dir=tmp,
                                 delay_ms=500, headed=False,
                                 filter_series=None, progress_cb=cb,
                                 webp=True)
            case_data_list.append(results)   # one group (list) per URL → one card
            slug = url.rstrip("/").split("/")[-1]
            rid  = results[0]["rid"] if results else "?"
            n_v  = len(results)
            cb({"type": "item_done",
                "message": f"Case {rid} \u2014 {slug}"
                           + (f" ({n_v} viewers)" if n_v > 1 else "")})
            done += 1

        # ---- Build package ----
        cb({"type": "progress", "message": "Building Anki package\u2026", "pct": 92})
        apkg_path = build_package(
            folder_title=folder_title,
            article_data_list=article_data_list,
            case_data_list=case_data_list,
            output_dir=tmp,
        )

        job["status"]    = "done"
        job["apkg_path"] = apkg_path
        cb({"type": "done",
            "download_url": f"/download/{job_id}",
            "filename":     apkg_path.name})

    except Exception as exc:
        job["status"] = "error"
        cb({"type": "error", "message": str(exc)})

    finally:
        _allow_sleep(_wake_handle)

        # Schedule cleanup after CLEANUP_DELAY_S
        def _cleanup() -> None:
            time.sleep(CLEANUP_DELAY_S)
            shutil.rmtree(str(tmp), ignore_errors=True)
            jobs.pop(job_id, None)

        threading.Thread(target=_cleanup, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    folder_title  = request.form.get("folder_title", "").strip()
    article_urls  = [u.strip() for u in request.form.get("article_urls", "").splitlines()
                     if u.strip()]
    case_urls     = [u.strip() for u in request.form.get("case_urls", "").splitlines()
                     if u.strip()]
    try:
        max_cases_per_article = int(request.form.get("max_cases_per_article", 4))
        if max_cases_per_article < 1:
            max_cases_per_article = None   # 0 or negative = unlimited
    except (ValueError, TypeError):
        max_cases_per_article = 4
    cases_from_articles = request.form.get("cases_from_articles") == "1"
    try:
        max_quiz_cases_per_article = int(request.form.get("max_quiz_cases_per_article", 4))
        if max_quiz_cases_per_article < 1:
            max_quiz_cases_per_article = None   # 0 = all
    except (ValueError, TypeError):
        max_quiz_cases_per_article = 4

    if not folder_title:
        return jsonify({"error": "Deck folder title is required."}), 400
    if not article_urls and not case_urls:
        return jsonify({"error": "Please enter at least one URL."}), 400

    # Validate URLs
    for url in article_urls + case_urls:
        if "radiopaedia.org" not in url:
            return jsonify({"error": f"Not a Radiopaedia URL: {url}"}), 400

    job_id  = uuid.uuid4().hex[:12]
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"radio_{job_id}_"))
    q       = queue.Queue()

    jobs[job_id] = {
        "id":         job_id,
        "status":     "running",
        "queue":      q,
        "apkg_path":  None,
        "tmp_dir":    tmp_dir,
        "created_at": time.time(),
    }

    threading.Thread(
        target=_run_job,
        args=(job_id, folder_title, article_urls, case_urls,
              max_cases_per_article, cases_from_articles,
              max_quiz_cases_per_article),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job ID."}), 404

    def generate():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=25)
            except queue.Empty:
                yield "data: {\"type\": \"heartbeat\"}\n\n"
                continue
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg.get("type") in ("done", "error"):
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # required for nginx
        },
    )


@app.route("/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["apkg_path"]:
        return jsonify({"error": "File not ready or job not found."}), 404

    return send_file(
        job["apkg_path"],
        as_attachment=True,
        download_name=job["apkg_path"].name,
        mimetype="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Radiopaedia → Anki web app")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Starting server at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
