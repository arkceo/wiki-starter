#!/usr/bin/env python3
"""wiki_server.py — serve the wiki to this Mac, and only to this Mac.

Run by a LaunchAgent with KeepAlive. Serves:

  /                  the built site in public/ (Quartz clean URLs: /page -> page.html,
                     /folder/ -> folder/index.html)
  /raw/<path>        source documents, read-only, so "Sources" links open the original
  /archive/<path>    processed Update Packets, read-only

It binds to 127.0.0.1, so nothing on the network can reach it. Requests that try to
escape those three roots (../, encoded or not, or a symlink pointing outside) are refused.
Standard library only.

Usage: wiki_server.py [--port N]   (default: "port" in wiki.config.json, else 8765)
"""
import argparse
import http.server
import json
import mimetypes
import os
import posixpath
import socketserver
import sys
import urllib.parse

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIND = "127.0.0.1"
ROOTS = {"raw": "raw", "archive": "archive"}  # url prefix -> folder under WIKI_DIR
EXTRA_TYPES = {".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8"}
NOT_BUILT = (
    "<!doctype html><meta charset=utf-8><title>Wiki</title>"
    "<body style='font:16px -apple-system,sans-serif;max-width:36rem;margin:4rem auto'>"
    "<h1>The wiki is being built</h1><p>The first build takes a minute or two. "
    "Refresh this page shortly.</p></body>"
)


def default_port():
    try:
        with open(os.path.join(WIKI_DIR, "wiki.config.json"), encoding="utf-8") as f:
            return int(json.load(f).get("port") or 8765)
    except Exception:
        return 8765


def safe_join(base, rel):
    """Resolve rel under base. Return None if the result would leave base."""
    base_real = os.path.realpath(base)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or "\x00" in p or "\\" in p for p in parts):
        return None
    target = os.path.realpath(os.path.join(base_real, *parts))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "wiki"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep the LaunchAgent log quiet
        pass

    def _headers(self, status, ctype, length):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _send_bytes(self, status, ctype, body, head_only):
        self._headers(status, ctype, len(body))
        if not head_only:
            self.wfile.write(body)

    def _send_file(self, path, head_only, status=200):
        ext = os.path.splitext(path)[1].lower()
        ctype = EXTRA_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        size = os.path.getsize(path)
        self._headers(status, ctype, size)
        if head_only:
            return
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _not_found(self, head_only):
        page = safe_join(os.path.join(WIKI_DIR, "public"), "404.html")
        if page and os.path.isfile(page):
            return self._send_file(page, head_only, status=404)
        self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", head_only)

    def _resolve_site(self, rel):
        public = os.path.join(WIKI_DIR, "public")
        if not os.path.isdir(public):
            return "NOT_BUILT"
        candidates = []
        if rel == "" or rel.endswith("/"):
            candidates.append(rel + "index.html")
        else:
            candidates += [rel, rel + ".html", rel + "/index.html"]
        for c in candidates:
            p = safe_join(public, c)
            if p and os.path.isfile(p):
                return p
        return None

    def _handle(self, head_only):
        raw_path = urllib.parse.urlsplit(self.path).path
        rel = urllib.parse.unquote(raw_path).lstrip("/")
        # Normalise without letting ".." climb (safe_join rejects it anyway).
        if rel and posixpath.normpath(rel).startswith(".."):
            return self._send_bytes(403, "text/plain; charset=utf-8", b"Forbidden\n", head_only)
        top = rel.split("/", 1)[0]
        if top in ROOTS:
            sub = rel.split("/", 1)[1] if "/" in rel else ""
            p = safe_join(os.path.join(WIKI_DIR, ROOTS[top]), sub)
            if p is None:
                return self._send_bytes(403, "text/plain; charset=utf-8", b"Forbidden\n", head_only)
            if os.path.isfile(p):
                return self._send_file(p, head_only)
            return self._not_found(head_only)
        p = self._resolve_site(rel)
        if p == "NOT_BUILT":
            return self._send_bytes(503, "text/html; charset=utf-8", NOT_BUILT.encode(), head_only)
        if p is None:
            return self._not_found(head_only)
        return self._send_file(p, head_only)

    def do_GET(self):
        try:
            self._handle(head_only=False)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        try:
            self._handle(head_only=True)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="Serve the wiki to this Mac only.")
    ap.add_argument("--port", type=int, default=default_port())
    a = ap.parse_args()
    httpd = Server((BIND, a.port), Handler)
    print(f"wiki: serving {WIKI_DIR} at http://{BIND}:{a.port}/", file=sys.stderr, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
