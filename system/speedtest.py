"""Authenticated, self-hosted OpenSpeedTest utility; no Docker or external server."""
from pathlib import Path

from flask import Blueprint, Response, abort, render_template, request, send_file, send_from_directory


speedtest_bp = Blueprint("speedtest_bp", __name__, url_prefix="/speedtest")
VENDOR = Path(__file__).resolve().parent / "vendor" / "openspeedtest"
MAX_UPLOAD = 35 * 1024 * 1024


@speedtest_bp.get("")
@speedtest_bp.get("/")
def index():
    return render_template("speedtest.html")


@speedtest_bp.get("/interface/")
def interface():
    return send_file(VENDOR / "index.html", max_age=0)


@speedtest_bp.get("/interface/assets/<path:filename>")
def assets(filename):
    return send_from_directory(VENDOR / "assets", filename, max_age=86400)


@speedtest_bp.get("/licence")
def license_text():
    return send_file(VENDOR / "License.md", mimetype="text/plain")


@speedtest_bp.route("/downloading", methods=["GET", "HEAD", "OPTIONS"])
def downloading():
    if request.method == "OPTIONS":
        return Response(status=200, headers={"Allow": "GET, HEAD, OPTIONS"})
    # A real, incompressible 30 MiB upstream payload. Stream from disk, rather
    # than constructing a large Python object for every parallel connection.
    response = send_file(VENDOR / "downloading", mimetype="application/octet-stream",
                         conditional=False, max_age=0, etag=False)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, no-transform"
    response.headers["Content-Encoding"] = "identity"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@speedtest_bp.route("/upload", methods=["GET", "HEAD", "POST", "OPTIONS"])
def upload():
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            abort(403)
        request.max_content_length = MAX_UPLOAD
        if request.content_length is not None and request.content_length > MAX_UPLOAD:
            abort(413)
        # Consume the upload completely before acknowledging it. Never persist
        # uploaded data and never buffer the full 30 MiB request in memory.
        while request.stream.read(64 * 1024):
            pass
    return Response(status=200, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Allow": "GET, HEAD, POST, OPTIONS",
        "X-Accel-Buffering": "no",
    })
