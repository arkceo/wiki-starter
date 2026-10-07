#!/usr/bin/env python3
"""wiki_cloud.py — run the wiki on a Linux server behind Nucleus Cloud, not on a Mac.

On a Mac the installer sets the wiki up and two LaunchAgents keep it going. A Nucleus
Cloud computer has neither: its image carries this engine (read-only, with Node's and
Python's dependencies installed beside it), and the account's own disk holds the wiki.
Claude reads through Nucleus credits ("engine": "credits"; scripts/wiki_claude.py), so no
Anthropic sign-in is stored anywhere.

  wiki_cloud.py setup --wiki DIR [--title T] [--company C]
      A new wiki in DIR from this engine (DIR missing or empty), or, when an existing
      one's version differs, this engine's files brought into it the way the Mac updater
      does; never touches wiki/, raw/, archive/ or the settings. Then the site is built if
      it has never been.
  wiki_cloud.py serve --wiki DIR
      The viewer (scripts/wiki_server.py, on 127.0.0.1 as on a Mac) kept running, and the
      runner started whenever the queue changes and every 15 minutes, as the LaunchAgents
      would.

Environment:
  WIKI_DEPS             where the image installed node_modules/ and venv/ (default
                        /opt/wiki-deps); the wiki links to them
  WIKI_SITE_BASE, WIKI_APP_BASE
                        the paths the proxy serves the site and the Upload app under
                        (e.g. /wiki and /wiki/_app); passed to the viewer and the site build
  WIKI_CREDITS_URL, WIKI_CREDITS_TOKEN
                        the credits service Claude is reached through (scripts/wiki_claude.py)

Standard library only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {".git", "node_modules", ".venv", "public", "cache", ".quartz-cache"}
QUEUES = ("raw/_intake", "raw/inbox")
IGNORED = re.compile(r"^(\..*|Icon.*|README\.md|.*\.(download|crdownload|part|partial|tmp)|~\$.*)$")
EVERY_SECONDS = 900      # the LaunchAgent's StartInterval
LOOK_SECONDS = 5         # how often the queue is looked at


def deps():
    return os.environ.get("WIKI_DEPS") or "/opt/wiki-deps"


def say(msg):
    print(f"wiki-cloud: {msg}", file=sys.stderr, flush=True)


def version(d):
    try:
        return open(os.path.join(d, ".wiki-engine", "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        return ""


def link_deps(wiki):
    """node_modules and .venv in the wiki point at the image's copies."""
    for name, target in (("node_modules", os.path.join(deps(), "node_modules")),
                         (".venv", os.path.join(deps(), "venv"))):
        p = os.path.join(wiki, name)
        if not os.path.isdir(target):
            continue
        if os.path.islink(p) and os.readlink(p) == target:
            continue
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
        os.symlink(target, p)


def git(wiki, *args):
    return subprocess.run(["git", "-C", wiki, *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True)


def write_config(wiki, title, company):
    p = os.path.join(wiki, "wiki.config.json")
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.update(title=title or "My Wiki", company=company or "", port=int(cfg.get("port") or 8765),
               engine="credits", pipeline="fast")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    if company:
        rules = os.path.join(wiki, "HOUSE-RULES.md")
        try:
            s = open(rules, encoding="utf-8").read()
            s = re.sub(r"^- Company name:\s*$", lambda _: "- Company name: " + company, s, count=1, flags=re.M)
            open(rules, "w", encoding="utf-8").write(s)
        except OSError:
            pass


def runner_env(wiki):
    env = dict(os.environ)
    env.setdefault("WIKI_LOG_DIR", os.path.join(wiki, ".wiki-engine", "logs"))
    env.setdefault("WIKI_PYTHON", os.path.join(wiki, ".venv", "bin", "python"))
    env.setdefault("WIKI_NOTIFY_CMD", "true")    # no notification centre on a server
    return env


def setup(wiki, title="", company=""):
    wiki = os.path.abspath(wiki)
    have, new = version(wiki), version(ENGINE)
    if not new:
        raise SystemExit("this folder is not the wiki engine (no .wiki-engine/VERSION)")
    if not have:
        if os.path.exists(wiki) and os.listdir(wiki):
            raise SystemExit(f"{wiki} is not empty and is not a wiki")
        say(f"setting up a new wiki ({new}) in {wiki}")
        shutil.copytree(ENGINE, wiki, symlinks=True, dirs_exist_ok=True,
                        ignore=lambda d, names: [n for n in names if d == ENGINE and n in SKIP])
        write_config(wiki, title, company)
        link_deps(wiki)
        os.makedirs(os.path.join(wiki, ".wiki-engine", "state"), exist_ok=True)
        if git(wiki, "init", "-q", "-b", "main").returncode:
            git(wiki, "init", "-q")
        git(wiki, "add", "-A", "--", ".")
        git(wiki, "-c", "user.name=Wiki installer", "-c", "user.email=installer@localhost",
            "commit", "-q", "-m", f"install: wiki-starter {new}")
    elif have != new:
        say(f"updating the engine from {have} to {new}")
        update(wiki)
    link_deps(wiki)
    if not os.path.isfile(os.path.join(wiki, "public", "index.html")):
        say("building the site")
        subprocess.run(["/bin/bash", "scripts/wiki_runner.sh", "--rebuild"], cwd=wiki, env=runner_env(wiki),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


PROTECTED = re.compile(r"^(wiki/|raw/|archive/|\.git/|\.wiki-engine/(backup|state)/|/|.*\.\./)"
                       r"|^(index\.md|log\.md|HOUSE-RULES\.md|wiki\.config\.json)$")


def manifest(d):
    """.wiki-engine/files.txt: {path: hash}."""
    out = {}
    try:
        for line in open(os.path.join(d, ".wiki-engine", "files.txt"), encoding="utf-8"):
            h, sep, path = line.rstrip("\n").partition("  ")
            if sep and path:
                out[path] = h
    except OSError:
        pass
    return out


def protected(path):
    """What an engine update must never write, as the Mac updater (Update Wiki Engine.command)
    decides it."""
    if path.startswith("generated/"):
        return not path.startswith("generated/_templates/")
    return bool(PROTECTED.match(path))


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def update(wiki):
    """The Mac updater's work, from the engine in the image: add or replace the engine's
    files, remove those it no longer ships, keep an edited copy in .wiki-engine/backup/,
    copy the bookkeeping, and commit the engine's files alone. The dependencies are the
    image's own, so nothing is installed here."""
    old, new = manifest(wiki), manifest(ENGINE)
    if not new:
        raise SystemExit("the engine has no .wiki-engine/files.txt")
    backup = os.path.join(wiki, ".wiki-engine", "backup", time.strftime("%Y%m%d-%H%M%S"))

    def keep(rel):
        dest = os.path.join(backup, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(wiki, rel), dest)

    for rel, h in new.items():
        if protected(rel):
            continue
        cur = os.path.join(wiki, rel)
        if os.path.isfile(cur):
            now = sha256(cur)
            if now == h:
                continue
            if now != old.get(rel):
                keep(rel)
        os.makedirs(os.path.dirname(cur), exist_ok=True)
        shutil.copy2(os.path.join(ENGINE, rel), cur)
    for rel, h in old.items():
        cur = os.path.join(wiki, rel)
        if rel in new or protected(rel) or not os.path.isfile(cur):
            continue
        if sha256(cur) != h:
            keep(rel)
        os.unlink(cur)
    for f in ("files.txt", "VERSION", "CHANGELOG.md", "source"):
        src = os.path.join(ENGINE, ".wiki-engine", f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(wiki, ".wiki-engine", f))
    if os.path.isdir(os.path.join(wiki, ".git")):
        git(wiki, "add", "-A", "--", ".", ":!wiki", ":!raw", ":!archive", ":!generated", ":!index.md", ":!log.md",
            ":!HOUSE-RULES.md", ":!wiki.config.json")
        if os.path.isdir(os.path.join(wiki, "generated", "_templates")):
            git(wiki, "add", "-A", "--", "generated/_templates")
        git(wiki, "-c", "user.name=Wiki updater", "-c", "user.email=updater@localhost",
            "commit", "-q", "-m", f"update: wiki-starter {version(ENGINE)}")
    # The runner applies the new engine's own fixes (and seeds) on a rebuild.
    subprocess.run(["/bin/bash", "scripts/wiki_runner.sh", "--rebuild"], cwd=wiki, env=runner_env(wiki),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pending(wiki):
    """The files waiting in the two queues (as the runner lists them), as a sorted tuple."""
    out = []
    for q in QUEUES:
        top = os.path.join(wiki, q)
        for d, dirs, files in os.walk(top):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            out += [os.path.join(d, f) for f in files if not IGNORED.match(f)]
    return tuple(sorted(out))


def serve(wiki):
    wiki = os.path.abspath(wiki)
    env = runner_env(wiki)
    py = env["WIKI_PYTHON"] if os.path.exists(env["WIKI_PYTHON"]) else sys.executable
    viewer = runner = None
    last_seen, last_run = None, 0.0
    while True:
        if viewer is None or viewer.poll() is not None:
            if viewer is not None:
                say(f"the viewer stopped (exit {viewer.returncode}); starting it again")
                time.sleep(2)
            viewer = subprocess.Popen([py, os.path.join(wiki, "scripts", "wiki_server.py")], cwd=wiki, env=env,
                                      stdin=subprocess.DEVNULL)
        now, waiting = time.time(), pending(wiki)
        idle = runner is None or runner.poll() is not None
        if idle and ((waiting and waiting != last_seen) or now - last_run >= EVERY_SECONDS):
            runner = subprocess.Popen(["/bin/bash", os.path.join(wiki, "scripts", "wiki_runner.sh")], cwd=wiki,
                                      env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
            last_run = now
        if idle:
            last_seen = waiting
        time.sleep(LOOK_SECONDS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the wiki behind Nucleus Cloud.")
    ap.add_argument("command", choices=["setup", "serve"])
    ap.add_argument("--wiki", required=True, help="the wiki's folder")
    ap.add_argument("--title", default="")
    ap.add_argument("--company", default="")
    a = ap.parse_args(argv)
    if a.command == "setup":
        return setup(a.wiki, a.title, a.company)
    return serve(a.wiki)


if __name__ == "__main__":
    sys.exit(main())
