#!/usr/bin/env python3
"""Startup-contract spec for the real sidecar, on any OS, with no credentials.

    uv run test/handshake_spec.py

Launches sidecar.py exactly as init.lua does, pointed at a synthesized Cursor
install (see fake_cursor_home.py), and asserts the parts that are platform-
specific and therefore easy to break:

  * the resolver finds Cursor where this OS puts it
  * SQLite opens state.vscdb through a file:// URI (backslashes, drive letters,
    the space in "Application Support")
  * stdout is framed with LF, never CRLF — Windows text mode would break the
    client's line splitting
  * a request still gets a framed reply (an auth error here, since the token is
    fake) rather than killing the process
  * a missing install exits 1 with an actionable message, not a traceback

The live backend is deliberately out of scope: that path is httpx over TLS and
is not platform-specific.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))
import fake_cursor_home  # noqa: E402

# Same launch shape as init.lua's sidecar_cmd; override for a preinstalled env.
_override = os.environ.get("NC_SIDECAR_CMD")
CMD = (_override.split() if _override else ["uv", "run", "--with", "httpx[http2]"]) + [
    str(ROOT / "sidecar.py")
]

_fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got  {got!r}\n       want {want!r}")
        _fails.append(name)


class Sidecar:
    """Line-pumped subprocess; keeps raw bytes so CRLF is observable."""

    def __init__(self, cursor_dir):
        env = dict(os.environ, CURSOR_CONFIG_DIR=str(cursor_dir))
        env.pop("CURSOR_STATE_DB_PATH", None)
        self.p = subprocess.Popen(
            CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.q = queue.Queue()
        for tag, stream in (("out", self.p.stdout), ("err", self.p.stderr)):
            threading.Thread(target=self._pump, args=(tag, stream), daemon=True).start()

    def _pump(self, tag, stream):
        for raw in iter(stream.readline, b""):
            self.q.put((tag, raw))
        self.q.put((tag, None))

    def wait_for(self, tag, pred, timeout=60):
        """Next line on `tag` matching pred (raw bytes), or None on timeout."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                t, raw = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if raw is None:  # stream closed; the other one may still speak
                continue
            if t == tag and pred(raw):
                return raw
        return None

    def send(self, obj):
        assert self.p.stdin, "stdin pipe closed"
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def close(self):
        self.p.kill()
        self.p.wait(timeout=10)


print(f"launch: {' '.join(CMD)}")

# --- happy path: synthesized install ----------------------------------------
with tempfile.TemporaryDirectory() as td:
    cursor_dir = Path(td) / "Cursor"
    fake_cursor_home.build(cursor_dir)
    print(f"synthesized install — {cursor_dir}")

    sc = Sidecar(cursor_dir)
    try:
        ready = sc.wait_for("err", lambda b: b"ready" in b, timeout=120)
        check("sidecar reaches ready", ready is not None)

        cfg = sc.wait_for("out", lambda b: b.strip().startswith(b"{"), timeout=60)
        check("emits a config line on stdout", cfg is not None)
        if cfg:
            # The Windows assertion: text mode would have made this b"}\r\n".
            check("stdout framed with LF, not CRLF", cfg.endswith(b"\n") and not cfg.endswith(b"\r\n"))
            check("config line is JSON with a config key", "config" in json.loads(cfg.decode()))

        sc.send(
            {
                "id": 42,
                "path": "x.py",
                "content": "def f():\n    pass\n",
                "line": 1,
                "col": 8,
                "language": "python",
            }
        )
        reply = sc.wait_for("out", lambda b: b'"id": 42' in b or b'"id":42' in b, timeout=90)
        check("request gets a framed reply", reply is not None)
        if reply:
            j = json.loads(reply.decode())
            # A fake token can only produce an error — what matters is that the
            # process framed it and stayed alive instead of dying.
            check("reply is an error (fake token), not a crash", "error" in j or "text" in j)
            check("process still alive after the error", sc.p.poll() is None)
    finally:
        sc.close()

# --- failure path: nothing installed ----------------------------------------
with tempfile.TemporaryDirectory() as td:
    print("missing install")
    env = dict(os.environ, CURSOR_CONFIG_DIR=td)
    env.pop("CURSOR_STATE_DB_PATH", None)
    r = subprocess.run(CMD, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=300)
    err = r.stderr.decode(errors="replace")
    check("exits non-zero", r.returncode != 0)
    check("names state.vscdb", "state.vscdb" in err)
    check("points at the override", "CURSOR_CONFIG_DIR" in err)
    check("no python traceback", "Traceback" not in err)

print(f"\n{'FAILED: ' + ', '.join(_fails) if _fails else 'handshake contract holds'}")
sys.exit(1 if _fails else 0)
