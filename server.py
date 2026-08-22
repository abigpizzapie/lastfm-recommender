#!/usr/bin/env python3
"""
Web server for the Last.fm dashboard.

Run this instead of `python3 lastfm_recommender.py` directly when you
want the "Refresh Recommendations" button on the dashboard to work
(e.g. when it's sitting behind a reverse proxy like nginx + tinyauth).

    python3 server.py

Point your reverse proxy at this process's port (default 8080). The
first request will generate a dashboard if one doesn't exist yet;
after that, the button on the page triggers a background refresh.
"""

import threading
import time
import traceback

from flask import Flask, Response, redirect, request, jsonify

import lastfm_recommender as lr

app = Flask(__name__)

_lock = threading.Lock()
_state = {"status": "idle", "error": None, "last_run": None}


def _run_refresh():
    if _lock.locked():
        return
    with _lock:
        _state["status"] = "running"
        _state["error"] = None
        try:
            config = lr.load_config()
            lr.generate(config)
            _state["last_run"] = time.strftime("%Y-%m-%d %H:%M")
        except SystemExit as e:
            _state["error"] = str(e)
        except Exception:
            _state["error"] = traceback.format_exc(limit=3)
        finally:
            _state["status"] = "idle"


@app.route("/")
def index():
    if not lr.OUTPUT_HTML.exists():
        # first ever run: generate synchronously so there's something to show
        _run_refresh()
        if _state["error"]:
            return Response(
                f"<pre>Setup failed:\n{_state['error']}</pre>"
                f"<p>Check config.json, then reload this page.</p>",
                mimetype="text/html",
                status=500,
            )
        return redirect("/")

    content = lr.OUTPUT_HTML.read_text(encoding="utf-8")

    if request.args.get("error"):
        banner = (
            '<div style="max-width:900px;margin:24px auto 0;padding:14px 18px;'
            'background:#3a1f1f;border:1px solid #6b2c2c;color:#f3d6d6;'
            'font-family:monospace;font-size:13px;border-radius:4px;">'
            "Last refresh failed \u2014 check the server logs.</div>"
        )
        content = content.replace("<body>", "<body>" + banner, 1)

    return Response(content, mimetype="text/html")


@app.route("/refresh", methods=["POST"])
def refresh():
    if _state["status"] != "running":
        threading.Thread(target=_run_refresh, daemon=True).start()
    return redirect("/refreshing")


@app.route("/refreshing")
def refreshing():
    return Response(REFRESHING_HTML, mimetype="text/html")


@app.route("/status")
def status():
    return _state


@app.route("/block-track", methods=["POST"])
def block_track():
    """Block a track from future recommendations."""
    try:
        data = request.get_json()
        track_key = data.get("track_key")
        
        if not track_key:
            return jsonify({"error": "Missing track_key"}), 400
        
        blocked = lr.load_blocked_tracks()
        blocked.add(track_key)
        lr.save_blocked_tracks(blocked)
        
        return jsonify({"success": True, "track_key": track_key}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


REFRESHING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Refreshing\u2026</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  body { margin:0; background:#15120f; color:#efe7d8; font-family:'Inter',system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; text-align:center; }
  h1 { font-family:'Fraunces',serif; font-size:28px; margin-bottom:8px; }
  p { color:#a89e8c; font-size:14px; }
</style>
</head>
<body>
<div>
  <h1>Refreshing your recommendations\u2026</h1>
  <p>Walking Last.fm's similarity graph, usually takes 15\u201330 seconds.</p>
</div>
<script>
async function poll() {
  try {
    const r = await fetch('/status');
    const s = await r.json();
    if (s.status !== 'running') {
      window.location.href = s.error ? '/?error=1' : '/';
      return;
    }
  } catch (e) { /* ignore transient errors while server restarts */ }
  setTimeout(poll, 2000);
}
poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
