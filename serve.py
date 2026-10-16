#!/usr/bin/env python3
"""Statische server voor de SVG-editor + VTracer conversie-endpoint.

Gebruik:
    python3 serve.py [poort]        # standaard 8090
    open daarna http://<host>:8090/

Endpoints:
    GET  /                  -> de editor (index.html)
    GET  /api/health        -> {"vtracer": true|false}
    POST /convert?preset=poster
         body = de afbeelding (png/jpg/webp/bmp)
         -> image/svg+xml (getraceerd met VTracer)

Vereist: pip install vtracer   (Rust-wheel, geen systeemlibs nodig)
De engine is dezelfde als in visioncortex/vtracer en lorrisc/PixelToPath.
"""
import http.server
import socketserver
import os
import sys
import json
import shutil
import subprocess
import urllib.parse
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))

# Presets (vrij naar PixelToPath: B&W / Poster / Photo)
PRESETS = {
    "poster": dict(colormode="color", hierarchical="stacked", mode="spline",
                   filter_speckle=4, color_precision=6, layer_difference=16),
    "bw":     dict(colormode="binary", hierarchical="stacked", mode="spline",
                   filter_speckle=4),
    "photo":  dict(colormode="color", hierarchical="stacked", mode="spline",
                   filter_speckle=4, color_precision=8, layer_difference=48),
}

try:
    import vtracer as _vtracer
except Exception:
    _vtracer = None


def guess_ext(data, content_type):
    """VTracer bepaalt het formaat aan de bestandsextensie -> die moet kloppen."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if data[4:8] == b"ftyp":
        return ".avif"           # heic/avif; VTracer faalt -> ImageMagick-fallback
    ct = (content_type or "").lower()
    for needle, ext in (("png", ".png"), ("jpeg", ".jpg"), ("jpg", ".jpg"),
                        ("webp", ".webp"), ("gif", ".gif"), ("bmp", ".bmp"),
                        ("tiff", ".tiff"), ("heic", ".avif"), ("avif", ".avif")):
        if needle in ct:
            return ext
    return ".png"


def convert(src, dst, params):
    """VTracer; valt bij onbekende formaten terug op ImageMagick-normalisatie."""
    try:
        _vtracer.convert_image_to_svg_py(src, dst, **params)
        return
    except BaseException as first:  # pyo3-panics zijn BaseException, geen Exception
        conv = shutil.which("convert") or shutil.which("magick")
        if not conv:
            raise
        norm = os.path.join(os.path.dirname(src), "normalised.png")
        subprocess.run([conv, src, "-alpha", "on", "png:" + norm],
                       check=True, capture_output=True, timeout=120)
        sys.stderr.write("[serve] VTracer-fallback via ImageMagick (%s)\n" % first)
        _vtracer.convert_image_to_svg_py(norm, dst, **params)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] %s\n" % (fmt % args))

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/api/health":
            self._send_json({"vtracer": _vtracer is not None})
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/convert":
            self.send_error(404, "onbekend endpoint")
            return
        if _vtracer is None:
            self.send_error(503, "vtracer niet geinstalleerd: pip install vtracer")
            return

        query = urllib.parse.parse_qs(parsed.query)
        preset = query.get("preset", ["poster"])[0]
        params = dict(PRESETS.get(preset, PRESETS["poster"]))
        for key in ("colormode", "hierarchical", "mode"):
            if key in query:
                params[key] = query[key][0]

        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length) if length else b""
        if not data:
            self.send_error(400, "lege body")
            return

        ext = guess_ext(data, self.headers.get("Content-Type"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = os.path.join(tmp, "in" + ext)
                dst = os.path.join(tmp, "out.svg")
                with open(src, "wb") as fh:
                    fh.write(data)
                convert(src, dst, params)
                with open(dst, "r", encoding="utf-8") as fh:
                    svg = fh.read()
        except BaseException as exc:  # pyo3-panics zijn BaseException, geen Exception
            sys.stderr.write("[serve] conversie faalde (ext=%s, ctype=%s, %d bytes): %s\n"
                             % (ext, self.headers.get("Content-Type"), len(data), exc))
            self.send_error(500, "conversie mislukt: %s" % exc)
            return

        body = svg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as httpd:
        print("SVG-editor + VTracer op http://0.0.0.0:%d  (vtracer: %s)"
              % (port, "aan" if _vtracer else "UIT - pip install vtracer"))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
