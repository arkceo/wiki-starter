#!/usr/bin/env python3
"""wiki_netguard.py — keep the engine's own Python off the local network.

The wiki talks only to this Mac (127.0.0.1) and, with Claude, to Anthropic. macOS asks
before a program reaches other devices on the local network ("Allow Python to find
devices on local networks?"), and looking up this Mac's own name, any *.local name, or an
address's name counts. The engine needs none of that, so it refuses it in-process, before
macOS is ever asked:

  - turning an address back into a name (gethostbyaddr, getnameinfo);
  - looking up a *.local name or this Mac's own name;
  - connecting or sending to a local-network address (private, link-local, multicast)
    other than this Mac itself.

A refusal raises the same error a failed lookup or connection would, which the standard
library already handles, and is written once to the runner log with the code that asked,
so an unexpected caller is easy to find. Set WIKI_NETGUARD=0 to turn it off.

Standard library only.
"""
import datetime
import errno
import ipaddress
import os
import socket
import sys
import threading
import traceback

LOG_FILE = os.path.join(
    os.environ.get("WIKI_LOG_DIR") or os.path.expanduser("~/Library/Logs/wiki-starter"), "runner.log"
)

_installed = False
_reported = set()
_own_names = set()
_busy = threading.local()   # per thread: the guard's own work is not checked again,
                            # while another thread's socket calls still are


def _host_names():
    names = set()
    try:
        host = socket.gethostname().strip().lower().rstrip(".")
    except OSError:
        host = ""
    if host:
        short = host.split(".")[0]
        names.update({host, short, short + ".local"})
    return names


def _ip(host):
    try:
        return ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return None


def local_name(host):
    """True for a name only the local network can answer: *.local or this Mac's own."""
    if not isinstance(host, str):
        if isinstance(host, (bytes, bytearray)):
            host = bytes(host).decode("ascii", "replace")
        else:
            return False
    h = host.strip().lower().rstrip(".")
    if not h or h == "localhost" or _ip(h) is not None:
        return False
    return h.endswith(".local") or h in _own_names


def local_address(host):
    """True for an address on the local network that is not this Mac itself."""
    ip = _ip(host)
    if ip is None:
        return local_name(host)
    if ip.is_loopback or ip.is_unspecified:
        return False
    return ip.is_private or ip.is_link_local or ip.is_multicast


def _report(what, target):
    key = (what, str(target))
    if key in _reported:
        return
    _reported.add(key)
    here = os.path.abspath(__file__)
    frames = [f for f in traceback.extract_stack()[:-3] if os.path.abspath(f.filename) != here][-3:]
    where = " > ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in frames) or "unknown"
    line = (f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} network guard: refused {what} {target} "
            f"(the wiki never uses the local network; asked by {where})\n")
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _refuse(what, target, exc):
    _report(what, target)
    raise exc


def _hook(event, args):
    if not event.startswith("socket.") or getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        if event == "socket.gethostbyaddr":
            _refuse("a reverse lookup of", args[0],
                    socket.herror(1, "refused: the wiki never looks up names on the local network"))
        elif event == "socket.getnameinfo":
            host = args[0][0] if args and isinstance(args[0], tuple) and args[0] else args[0]
            _refuse("a reverse lookup of", host,
                    socket.gaierror(socket.EAI_NONAME, "refused: the wiki never looks up names on the local network"))
        elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
            if args and local_name(args[0]):
                _refuse("a lookup of", args[0],
                        socket.gaierror(socket.EAI_NONAME, "refused: the wiki never looks up names on the local network"))
        elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            addr = args[1] if len(args) > 1 else None
            if isinstance(addr, tuple) and addr and local_address(addr[0]):
                _refuse("a connection to", addr[0],
                        OSError(errno.EHOSTUNREACH, "refused: the wiki never connects to the local network"))
    finally:
        _busy.on = False


def install():
    """Install the guard in this process (once; it cannot be removed afterwards)."""
    global _installed, _own_names
    if _installed or os.environ.get("WIKI_NETGUARD") == "0":
        return
    _own_names = _host_names()
    sys.addaudithook(_hook)
    _installed = True
