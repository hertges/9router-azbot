"""Build stamp: proves WHICH code is actually running.

Stale deploys caused real confusion here (bugs already fixed in source
kept appearing because the server ran an older copy). Importing this
module snapshots two things, best-effort and crash-proof:

- git short hash (+ "-dirty" flag) when the source is a git checkout,
- the newest .py mtime under this package ("code 2026-09-12 20:10 UTC")
  — this works even for plain zip deploys with no git history.

Shown in the startup log (core.start) and in /stats (handlers_admin).
If the stamp doesn't move after a redeploy, the old process is still
running (or the files didn't copy) — restart, don't debug ghosts.
"""
import os
import subprocess
import time

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _git_hash():
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PKG_DIR, capture_output=True, text=True, timeout=5)
        h = (r.stdout or "").strip()
        if r.returncode != 0 or not h:
            return None
        d = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=_PKG_DIR, capture_output=True, text=True, timeout=5)
        if (d.stdout or "").strip():
            h += "+dirty"
        return h
    except Exception:
        return None


def _code_stamp():
    try:
        latest = 0.0
        for root, _, files in os.walk(_PKG_DIR):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    try:
                        latest = max(latest, os.path.getmtime(os.path.join(root, f)))
                    except OSError:
                        pass
        if latest:
            return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(latest))
    except Exception:
        pass
    return "?"


GIT_HASH = _git_hash()
CODE_STAMP = _code_stamp()


def build_line():
    bits = []
    if GIT_HASH:
        bits.append(f"git {GIT_HASH}")
    bits.append(f"code {CODE_STAMP}")
    return " • ".join(bits)
