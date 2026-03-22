"""
app.py  —  Radiopaedia → Anki web interface

Usage:
    python app.py            # development (port 5000)
    python app.py --port 8080
    gunicorn -w 1 app:app    # production (SINGLE worker required)

Deployable behind nginx — set X-Accel-Buffering: no for SSE support.
"""

import json
import queue
import shutil
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
# Background worker
# ---------------------------------------------------------------------------

def _run_job(job_id: str, folder_title: str,
             article_urls: list[str], case_urls: list[str]) -> None:
    """Runs in a daemon thread. Puts SSE events onto the job queue."""
    job = jobs[job_id]
    q   = job["queue"]
    tmp = job["tmp_dir"]

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
                                    progress_cb=cb)
            article_data_list.append(result)
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
                                 filter_series=None, progress_cb=cb)
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
        args=(job_id, folder_title, article_urls, case_urls),
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
