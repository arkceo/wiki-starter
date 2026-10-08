#!/usr/bin/env python3
"""cloud/job.py — one reading job on a wiki kept in S3 rather than on a disk.

A service that keeps its customers' wikis in S3 (oeru does, in AWS Malaysia) runs this in
a short-lived container built from cloud/Dockerfile, one job per wiki at a time. The
container has no inbound network, and the AWS credentials it is given should reach that
one wiki's prefix in the bucket and nothing else. It:

  1. pulls the wiki from `<prefix>src/` (and its history, `<prefix>git.tar.gz`);
  2. sets it up the first time, or brings in a newer engine (wiki_cloud.py setup);
  3. runs the runner until nothing waits in the queues, taking in files that arrive
     meanwhile, and stopping well before its time is up;
  4. pushes what changed back to `src/`, the built site to `site/`, deleting only what it
     pulled and is now gone (a file uploaded while it ran is never deleted);
  5. reports to the service (`WIKI_JOB_DONE_URL`) with the engine's version, and exits.

While it runs it keeps `<prefix>progress.json` up to date (a step name, a count and a
time; never a file name), so the service can show the owner what it is doing.

Environment, all set by the service (each WIKI_JOB_* is also read as OERU_JOB_*, the
names oeru's first image used): AWS_* (the job's own credentials), WIKI_JOB_BUCKET,
WIKI_JOB_PREFIX, WIKI_JOB_KMS_KEY, WIKI_JOB_MINUTES, WIKI_JOB_DONE_URL, WIKI_JOB_TOKEN,
WIKI_JOB_BYPASS (a header for a protected preview only); WIKI_SITE_BASE, WIKI_APP_BASE,
WIKI_CREDITS_URL, WIKI_CREDITS_TOKEN (wiki_cloud.py); WIKI_THEME (the site's colours).

NOTHING PERSONAL IN THE LOG. File names can name people: only counts are printed.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import shutil
import tarfile
import time
import urllib.request

import boto3
from botocore.config import Config

ENGINE = os.environ.get("WIKI_ENGINE_DIR") or "/opt/engine"
WIKI = os.environ.get("WIKI_JOB_WIKI") or os.environ.get("OERU_JOB_WIKI") or "/work/wiki"
sys.path.insert(0, os.path.join(ENGINE, "scripts"))
import wiki_cloud  # noqa: E402  (the engine's own setup, queue listing and runner environment)



def job_env(name, default=None):
    """WIKI_JOB_<name>, or OERU_JOB_<name> as oeru's first image read it."""
    v = os.environ.get("WIKI_JOB_" + name) or os.environ.get("OERU_JOB_" + name)
    if v is None and default is None:
        raise SystemExit(f"job: WIKI_JOB_{name} is not set")
    return v if v is not None else default


BUCKET = job_env("BUCKET")
PREFIX = job_env("PREFIX")
KMS = job_env("KMS_KEY")
STARTED = time.time()
# Stop starting new work ten minutes before oeru's limit, so there is time to push.
DEADLINE = STARTED + max(5, int(job_env("MINUTES", "50") or "50") - 10) * 60
# What is never stored: the image's dependencies, the build and its caches, and the history (kept as one archive).
SKIP = {".git", "node_modules", ".venv", "public", "cache", ".quartz-cache"}

def client():
    """S3 at the region's own address: an opt-in region such as Malaysia is not served by the
    global one, and an upload address the browser is given must name the region's."""
    region = os.environ.get("AWS_REGION") or "ap-southeast-5"
    return boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, s3={"addressing_style": "virtual"}))


s3 = client()
PUT = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": KMS}


def say(msg):
    print(f"job: {msg}", file=sys.stderr, flush=True)


def keys(prefix):
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            out[o["Key"][len(prefix):]] = o["ETag"]
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def local(top, skip=()):
    """{relative path: sha256} for every plain file under top (no symlinks, nothing in skip)."""
    out = {}
    for d, dirs, files in os.walk(top):
        dirs[:] = [x for x in dirs if not (d == top and x in skip) and x != "__pycache__"
                   and not os.path.islink(os.path.join(d, x))]
        for f in files:
            p = os.path.join(d, f)
            if not os.path.islink(p) and os.path.isfile(p):
                out[os.path.relpath(p, top).replace(os.sep, "/")] = sha256(p)
    return out


def safe(rel):
    parts = rel.split("/")
    return rel and not rel.startswith("/") and all(p not in ("", ".", "..") for p in parts)


# The ETag each pulled object had: an object is deleted only while it still has it, so a
# file uploaded again under the same name while the job ran is kept for the next job.
ETAGS = {}


def pull_src():
    """The wiki's files, and what was pulled: {rel: sha256}."""
    pulled = {}
    for rel, etag in keys(PREFIX + "src/").items():
        if not safe(rel):
            continue
        dest = os.path.join(WIKI, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        s3.download_file(BUCKET, PREFIX + "src/" + rel, dest)
        pulled[rel] = sha256(dest)
        ETAGS[rel] = etag
    try:
        body = s3.get_object(Bucket=BUCKET, Key=PREFIX + "git.tar.gz")["Body"].read()
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
            # Written by an earlier job on this account alone; the data filter where Python has it.
            if hasattr(tarfile, "data_filter"):
                t.extractall(WIKI, filter="data")
            else:
                t.extractall(WIKI)
    except s3.exceptions.NoSuchKey:
        pass
    return pulled


def take_new(pulled):
    """Files that reached the queues in S3 since the pull: brought down, and counted as pulled."""
    n = 0
    for q in wiki_cloud.QUEUES:
        for rel, etag in keys(f"{PREFIX}src/{q}/").items():
            full = f"{q}/{rel}"
            if full in pulled or not safe(full):
                continue
            dest = os.path.join(WIKI, full)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            s3.download_file(BUCKET, f"{PREFIX}src/{full}", dest)
            pulled[full] = sha256(dest)
            ETAGS[full] = etag
            n += 1
    return n


def run_once():
    """One pass of the runner, cut short at the deadline."""
    env = wiki_cloud.runner_env(WIKI)
    p = subprocess.Popen(["/bin/bash", os.path.join(WIKI, "scripts", "wiki_runner.sh")], cwd=WIKI, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    try:
        p.wait(timeout=max(30, DEADLINE - time.time()))
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 15)
        p.wait()


def unchanged(key, etag):
    try:
        return s3.head_object(Bucket=BUCKET, Key=key)["ETag"] == etag
    except s3.exceptions.ClientError:
        return False


def push(prefix, top, before, now, delete_only, etags=None):
    """Upload what differs from `before`; delete what is in `delete_only` and gone from `now`
    (with `etags`, only while the object is still the one that was pulled)."""
    up = gone = 0
    for rel, h in now.items():
        if before.get(rel) != h:
            s3.upload_file(os.path.join(top, rel), BUCKET, prefix + rel, ExtraArgs=PUT)
            up += 1
    stale = [rel for rel in delete_only if rel not in now and (etags is None or unchanged(prefix + rel, etags.get(rel)))]
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": prefix + r} for r in stale[i:i + 1000]], "Quiet": True})
        gone += len(stale[i:i + 1000])
    return up, gone


def manifest():
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=PREFIX + "manifest.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {}


def progress(step, waiting=None):
    """`<prefix>progress.json`: the step this job is on, for the owner's panel. Only a step
    name, a count and a time; never a file name. Best effort: a failed write never stops the job."""
    try:
        s3.put_object(Bucket=BUCKET, Key=PREFIX + "progress.json", **PUT, ContentType="application/json",
                      Body=json.dumps({"step": step, "waiting": waiting, "at": int(time.time())}).encode())
    except Exception:  # noqa: BLE001
        pass


def done(outcome):
    headers = {"Authorization": "Bearer " + job_env("TOKEN"), "Content-Type": "application/json"}
    bypass = job_env("BYPASS", "")
    if bypass:  # a Vercel preview behind Deployment Protection
        headers["x-vercel-protection-bypass"] = bypass
    body = {"outcome": outcome, "engine": wiki_cloud.version(ENGINE)}
    req = urllib.request.Request(job_env("DONE_URL"), method="POST", data=json.dumps(body).encode(),
                                 headers=headers)
    for wait in (0, 5, 15, 45):
        time.sleep(wait)
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return
        except OSError:
            continue
    say("oeru could not be told; it will close the job itself")


def main():
    os.makedirs(WIKI, exist_ok=True)
    outcome = "failed"
    try:
        progress("loading")
        old = manifest()
        pulled = pull_src()
        say(f"pulled {len(pulled)} file(s)")
        progress("preparing")
        if not wiki_cloud.version(WIKI) and os.listdir(WIKI):
            # Files arrived before the wiki was ever set up: set it up, then lay them back.
            os.rename(WIKI, WIKI + ".early")
            os.makedirs(WIKI)
            wiki_cloud.setup(WIKI, "Wiki")
            shutil.copytree(WIKI + ".early", WIKI, dirs_exist_ok=True)
        else:
            wiki_cloud.setup(WIKI, "Wiki")
        # As wiki_cloud's serve loop: another pass only when the queue has changed; a file
        # the runner leaves where it was waits for the next job instead of spinning this one.
        passes, last = 0, None
        while time.time() < DEADLINE:
            waiting = wiki_cloud.pending(WIKI)
            if passes and waiting == last:
                break
            progress("reading", len(waiting))
            run_once()
            passes, last = passes + 1, waiting
            take_new(pulled)
        say(f"{passes} runner pass(es)")

        progress("saving")
        src = local(WIKI, SKIP)
        up, gone = push(PREFIX + "src/", WIKI, pulled, src, pulled, ETAGS)
        say(f"src: {up} up, {gone} removed")
        site_before = old.get("site", {})
        site = local(os.path.join(WIKI, "public"))
        if site:
            # Removes only pages the last job wrote and this build no longer has.
            up, gone = push(PREFIX + "site/", os.path.join(WIKI, "public"), site_before, site, site_before)
            say(f"site: {up} up, {gone} removed")
        if os.path.isdir(os.path.join(WIKI, ".git")):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                t.add(os.path.join(WIKI, ".git"), arcname=".git")
            s3.put_object(Bucket=BUCKET, Key=PREFIX + "git.tar.gz", Body=buf.getvalue(), **PUT)
        s3.put_object(Bucket=BUCKET, Key=PREFIX + "manifest.json", **PUT,
                      Body=json.dumps({"site": site or site_before, "at": int(time.time())}).encode())
        outcome = "done" if site or site_before else "failed"
    except (Exception, SystemExit) as e:  # the type only: a message can carry a file name
        say(f"stopped: {type(e).__name__}")
    done(outcome)
    return 0 if outcome == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
