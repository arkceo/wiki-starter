#!/usr/bin/env python3
"""cloud/session.py — the wiki itself, running for its owner while they use it.

The same container as cloud/job.py, started instead with this script while the owner has
the wiki's Upload page (Review, Ask, Actions, Settings) open. It runs the engine exactly as
a Mac does, `wiki_cloud.py serve`: the viewer, the Upload app and the runner, from the
wiki kept in S3. Nothing in the journey is the service's own; the service only signs the
owner in and passes their requests through. It:

  1. pulls the wiki from `<prefix>src/` and sets it up, as a job does;
  2. starts `wiki_cloud.py serve`, which binds the site and the Upload app to 127.0.0.1;
  3. opens the one door in, HTTPS on WIKI_SESSION_PORT (8443) with a certificate made at
     start-up, and tells the service it is ready, with that certificate, so the service
     can trust this machine and nothing pretending to be it;
  4. lets a request through only with a gate token signed for this wiki's account
     (X-Wiki-Gate), routing WIKI_APP_BASE/... to the Upload app and WIKI_SITE_BASE/... to
     the site;
  5. takes uploads straight into S3 (the service's proxy caps request sizes): the Upload
     app asks the helper on 127.0.0.1 here for an address, the browser sends the file
     there, and the helper brings it down into the queue (WIKI_UPLOAD_HELPER);
  6. saves the wiki back to S3 every few minutes when it has changed, so the pages the
     service serves from S3 stay current;
  7. stops by itself: after WIKI_SESSION_IDLE_MINUTES (15) with no request from the owner
     and nothing being read, after WIKI_SESSION_HOURS (4) whatever happens, or when the
     container is told to stop. It saves first, tells the service, and exits.

The service answers WIKI_SESSION_URL (a POST with WIKI_JOB_TOKEN as the bearer) with
{"state": "ready", "cert": ...}, {"state": "credentials"} (fresh AWS credentials for this
wiki's prefix, which a long session outlives) and {"state": "ended", "outcome": ...}.

Environment: as cloud/job.py, plus WIKI_SESSION_URL, WIKI_GATE_SECRET, WIKI_GATE_ACCOUNT,
WIKI_SITE_BASE, WIKI_APP_BASE, and optionally WIKI_SESSION_PORT, WIKI_SESSION_IDLE_MINUTES,
WIKI_SESSION_HOURS.

NOTHING PERSONAL IN THE LOG, and nothing about the owner's requests: only counts and states.
"""
import base64
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job  # noqa: E402  (the S3 side: pull, compare, push; and the engine's wiki_cloud)

wiki_cloud = job.wiki_cloud
WIKI = job.WIKI
PREFIX = job.PREFIX

SECRET = (os.environ.get("WIKI_GATE_SECRET") or "").strip().encode()
ACCOUNT = (os.environ.get("WIKI_GATE_ACCOUNT") or "").strip()
PORT = int(os.environ.get("WIKI_SESSION_PORT") or "8443")
IDLE = float(os.environ.get("WIKI_SESSION_IDLE_MINUTES") or "15") * 60
LONGEST = float(os.environ.get("WIKI_SESSION_HOURS") or "4") * 3600
SITE_BASE = (os.environ.get("WIKI_SITE_BASE") or "").rstrip("/")
APP_BASE = (os.environ.get("WIKI_APP_BASE") or "").rstrip("/")
SITE_PORT, APP_PORT, HELPER_PORT = 8765, 8766, 8767
SAVE_EVERY = 180          # seconds between saves while something changed
CREDENTIALS_EVERY = 2400  # the service's credentials last an hour; ask again well before
UPLOADS = PREFIX + "uploads/"
CERT_DIR = "/tmp/wiki-session-tls"

TOKEN_RE = re.compile(r"^(\d+)\.(\d+)\.([A-Za-z0-9_-]+)$")
SEND = ("Accept", "Accept-Language", "Content-Type", "Content-Length", "Cache-Control", "Last-Event-ID",
        "Range", "If-None-Match", "If-Modified-Since", "X-Wiki-Token")
DROP = {"connection", "keep-alive", "transfer-encoding", "content-length"}
# The pages poll these on their own; a tab left open is not the owner using the wiki.
POLLS = ("/api/status", "/api/events", "/.wiki/build", "/upload")

STARTED = time.time()
last_active = time.time()
stopping = threading.Event()


def say(msg):
    print(f"session: {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ the service --------
def tell(body):
    """POST to the service; its JSON answer, or None."""
    headers = {"Authorization": "Bearer " + job.job_env("TOKEN"), "Content-Type": "application/json"}
    bypass = job.job_env("BYPASS", "")
    if bypass:  # a preview behind Deployment Protection
        headers["x-vercel-protection-bypass"] = bypass
    req = urllib.request.Request(os.environ["WIKI_SESSION_URL"], method="POST", data=json.dumps(body).encode(),
                                 headers=headers)
    for wait in (0, 3, 10, 30):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except (OSError, ValueError):
            continue
    say(f"the service did not answer ({body.get('state')})")
    return None


def refresh_credentials():
    got = tell({"state": "credentials"})
    if not got or not got.get("key"):
        return False
    os.environ["AWS_ACCESS_KEY_ID"] = got["key"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = got["secret"]
    os.environ["AWS_SESSION_TOKEN"] = got["token"]
    job.s3 = job.client()
    return True


# ---------------------------------------------------------------------- storage --------
HASHES = {}  # path -> (size, mtime, sha256): a save only reads files that changed


def local(top, skip=()):
    out = {}
    for d, dirs, files in os.walk(top):
        dirs[:] = [x for x in dirs if not (d == top and x in skip) and x != "__pycache__"
                   and not os.path.islink(os.path.join(d, x))]
        for f in files:
            p = os.path.join(d, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            st = os.stat(p)
            seen = HASHES.get(p)
            if not seen or seen[:2] != (st.st_size, st.st_mtime_ns):
                seen = (st.st_size, st.st_mtime_ns, job.sha256(p))
                HASHES[p] = seen
            out[os.path.relpath(p, top).replace(os.sep, "/")] = seen[2]
    return out


class Saved:
    """What S3 holds, as last pulled or pushed by this session."""

    def __init__(self, src, site):
        self.src, self.site, self.git = src, site, None
        self.lock = threading.Lock()

    def save(self):
        with self.lock:
            src = local(WIKI, job.SKIP)
            ours = {r for r in self.src if r not in job.ETAGS}  # written by this session
            pulled = {r: e for r, e in job.ETAGS.items() if r in self.src}
            up = gone = 0
            for rel, h in src.items():
                if self.src.get(rel) != h:
                    job.s3.upload_file(os.path.join(WIKI, rel), job.BUCKET, PREFIX + "src/" + rel, ExtraArgs=job.PUT)
                    job.ETAGS.pop(rel, None)
                    up += 1
            stale = [r for r in self.src if r not in src
                     and (r in ours or job.unchanged(PREFIX + "src/" + r, pulled.get(r)))]
            for i in range(0, len(stale), 1000):
                job.s3.delete_objects(Bucket=job.BUCKET, Delete={
                    "Objects": [{"Key": PREFIX + "src/" + r} for r in stale[i:i + 1000]], "Quiet": True})
                gone += len(stale[i:i + 1000])
            self.src = src
            site = local(os.path.join(WIKI, "public"))
            if site and site != self.site:
                job.push(PREFIX + "site/", os.path.join(WIKI, "public"), self.site, site, self.site)
                self.site = site
            git = os.path.join(WIKI, ".git")
            if os.path.isdir(git):
                stamp = tuple(sorted(local(git).items()))
                if stamp != self.git:
                    buf = io.BytesIO()
                    with tarfile.open(fileobj=buf, mode="w:gz") as t:
                        t.add(git, arcname=".git")
                    job.s3.put_object(Bucket=job.BUCKET, Key=PREFIX + "git.tar.gz", Body=buf.getvalue(), **job.PUT)
                    self.git = stamp
            job.s3.put_object(Bucket=job.BUCKET, Key=PREFIX + "manifest.json", **job.PUT,
                              Body=json.dumps({"site": self.site, "at": int(time.time())}).encode())
            if up or gone:
                say(f"saved: {up} up, {gone} removed")


# ------------------------------------------------------------------------- door --------
def token_ok(token):
    m = TOKEN_RE.match(token or "")
    if not SECRET or not ACCOUNT or not m or m.group(1) != ACCOUNT or int(m.group(2)) < time.time():
        return False
    want = hmac.new(SECRET, f"gate.{m.group(1)}.{m.group(2)}".encode(), hashlib.sha256).digest()
    want = base64.urlsafe_b64encode(want).rstrip(b"=").decode()
    return hmac.compare_digest(want, m.group(3))


def route(path):
    """(port, path on that server) for a path as the browser sees it; (None, None) if neither."""
    for base, port in ((APP_BASE, APP_PORT), (SITE_BASE, SITE_PORT)):
        if not base:
            continue
        if path == base or path.startswith((base + "/", base + "?")):
            return port, path[len(base):] or "/"
    if not SITE_BASE:
        return SITE_PORT, path
    return None, None


class Door(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # nothing about the owner's requests is logged
        pass

    def refuse(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def handle_any(self):
        global last_active
        if not token_ok(self.headers.get("X-Wiki-Gate")):
            return self.refuse(403)
        if self.command not in ("GET", "HEAD") and self.headers.get("X-Wiki-Same-Origin") != "1":
            return self.refuse(403)
        port, path = route(self.path)
        if port is None:
            return self.refuse(404)
        if self.command != "HEAD" and not path.split("?")[0].endswith(POLLS):
            last_active = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.refuse(400)
        body = self.rfile.read(length) if length > 0 else None
        headers = {h: self.headers[h] for h in SEND if self.headers.get(h) is not None}
        headers["Host"] = f"127.0.0.1:{port}"
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
            conn.request(self.command, path, body=body, headers=headers)
            res = conn.getresponse()
        except OSError:
            return self.refuse(502)
        self.send_response(res.status)
        for k, v in res.getheaders():
            if k.lower() not in DROP:
                self.send_header(k, v)
        if self.command == "HEAD" or res.status in (204, 304):
            self.send_header("Content-Length", "0")
            self.end_headers()
            conn.close()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = res.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.close_connection = True
        finally:
            conn.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = handle_any


# ------------------------------------------------------------------ upload helper ------
class Helper(BaseHTTPRequestHandler):
    """On 127.0.0.1 only, for wiki_server.py: an S3 address per upload, then the file back down."""

    def log_message(self, *args):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.answer(400, {"ok": False})
        if self.path == "/presign":
            key = UPLOADS + secrets.token_hex(16)
            headers = {"x-amz-server-side-encryption": "aws:kms",
                       "x-amz-server-side-encryption-aws-kms-key-id": job.KMS}
            url = job.s3.generate_presigned_url(
                "put_object", ExpiresIn=900, HttpMethod="PUT",
                Params={"Bucket": job.BUCKET, "Key": key, "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": job.KMS})
            return self.answer(200, {"url": url, "headers": headers, "key": key})
        if self.path == "/fetch":
            key, dest = str(body.get("key") or ""), str(body.get("dest") or "")
            tmp_dir = os.path.join(WIKI, ".wiki-engine", "state", "uploads") + os.sep
            if not re.fullmatch(re.escape(UPLOADS) + r"[0-9a-f]{32}", key) \
                    or not os.path.abspath(dest).startswith(tmp_dir):
                return self.answer(400, {"ok": False, "error": "not an upload"})
            try:
                size = job.s3.head_object(Bucket=job.BUCKET, Key=key)["ContentLength"]
                if size > int(body.get("limit") or 0):
                    job.s3.delete_object(Bucket=job.BUCKET, Key=key)
                    return self.answer(200, {"ok": False, "error": "larger than the limit"})
                job.s3.download_file(job.BUCKET, key, dest)
                job.s3.delete_object(Bucket=job.BUCKET, Key=key)
            except Exception:  # noqa: BLE001  (the type is all the log may say)
                return self.answer(200, {"ok": False, "error": "the file did not arrive"})
            return self.answer(200, {"ok": True, "bytes": size})
        self.answer(404, {"ok": False})


# ------------------------------------------------------------------------- main --------
def certificate():
    """A key and a self-signed certificate made here, now; the service is told which one."""
    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)
    key, crt = os.path.join(CERT_DIR, "key.pem"), os.path.join(CERT_DIR, "cert.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", key, "-out", crt, "-days", "2", "-subj", "/CN=wiki-session",
                    "-addext", "subjectAltName=DNS:wiki-session"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return key, crt


def listening(timeout=300):
    """Wait until the site and the Upload app both take connections: only then is it ready."""
    until = time.time() + timeout
    while time.time() < until and not stopping.is_set():
        try:
            for port in (SITE_PORT, APP_PORT):
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return True
        except OSError:
            time.sleep(1)
    return False


def reading():
    """The runner is working, or files wait that it has not tried yet."""
    return os.path.isdir(os.path.join(WIKI, ".wiki-engine", "state", "runner.lock"))


def serve_forever(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    os.makedirs(WIKI, exist_ok=True)
    outcome = "failed"
    viewer = None
    saved = None
    try:
        old = job.manifest()
        pulled = job.pull_src()
        say(f"pulled {len(pulled)} file(s)")
        if not wiki_cloud.version(WIKI) and os.listdir(WIKI):
            os.rename(WIKI, WIKI + ".early")
            os.makedirs(WIKI)
            wiki_cloud.setup(WIKI, "Wiki")
            shutil.copytree(WIKI + ".early", WIKI, dirs_exist_ok=True)
        else:
            wiki_cloud.setup(WIKI, "Wiki")
        saved = Saved(pulled, old.get("site", {}))

        env = dict(os.environ, WIKI_UPLOAD_HELPER=f"http://127.0.0.1:{HELPER_PORT}")
        os.environ["WIKI_UPLOAD_HELPER"] = env["WIKI_UPLOAD_HELPER"]
        viewer = subprocess.Popen(["python3", os.path.join(WIKI, "scripts", "wiki_cloud.py"), "serve", "--wiki", WIKI],
                                  cwd=WIKI, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        serve_forever(ThreadingHTTPServer(("127.0.0.1", HELPER_PORT), Helper))
        key, crt = certificate()
        door = ThreadingHTTPServer(("0.0.0.0", PORT), Door)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(crt, key)
        door.socket = tls.wrap_socket(door.socket, server_side=True)
        serve_forever(door)
        if not listening():
            raise SystemExit("the engine did not start")
        tell({"state": "ready", "cert": open(crt).read(), "engine": wiki_cloud.version(job.ENGINE)})
        say("ready")

        last_save = last_creds = time.time()
        while not stopping.is_set():
            stopping.wait(15)
            now = time.time()
            if now - last_creds >= CREDENTIALS_EVERY and refresh_credentials():
                last_creds = now
            if now - last_save >= SAVE_EVERY:
                saved.save()
                last_save = now
            if now - STARTED >= LONGEST:
                say("the longest session is over")
                break
            if now - last_active >= IDLE and not reading():
                say("idle")
                break
        outcome = "done"
    except (Exception, SystemExit) as e:  # the type only: a message can carry a file name
        say(f"stopped: {type(e).__name__}")
    finally:
        if viewer is not None:
            # The viewer, the serve loop and a runner (its own session): everything but this
            # process, which the container runs as its first. Then nothing writes while it saves.
            try:
                os.kill(-1, signal.SIGTERM) if os.getpid() == 1 else os.killpg(viewer.pid, signal.SIGTERM)
                viewer.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if saved is not None:
            try:
                saved.save()
            except Exception as e:  # noqa: BLE001
                say(f"the last save failed: {type(e).__name__}")
                outcome = "failed"
        tell({"state": "ended", "outcome": outcome, "engine": wiki_cloud.version(job.ENGINE)})
    return 0 if outcome == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
