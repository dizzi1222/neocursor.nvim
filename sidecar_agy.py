#!/usr/bin/env python3
"""
neocursor.nvim sidecar — host "antigravity" (route B): CLI agy as the backend.

Speaks the SAME neutral stdio protocol as sidecar.py, so the Lua engine is
untouched: accept / jump / chain / Esc-dismiss work identically. The only real
difference vs host "cursor" is the source of suggestions and the latency.

Protocol (one JSON object per line, both directions):
  in : {"id":N,"path":str,"content":str,"line":int0,"col":int0,"language":str,
        "additional_files"?:..., "linter_errors"?:..., "file_diff_histories"?:...}
  out: {"id":N,"edits":[{"text":str,"range":{"start":int1,"endInclusive":int1}}],
        "prediction":null}
       {"id":N,"error":str}

How it produces an edit (agy is a chat agent, not a ghost-streaming API):
  1. Build a temp copy of the current file with a marker at the cursor position.
  2. Invoke `agy --print` (headless, non-interactive) asking it to replace the
     marker with the natural completion and output ONLY the full file content.
  3. Diff marker-free original vs produced content → neutral `edits` list.

Resilience / quota guards:
  - Settle window: a request is only forwarded to agy once the editor has been
    quiet for SETTLE_MS. Bursts reply a noop fast (no quota burn), and the
    editor's own debounce re-fires the newest request when typing pauses.
  - Newest-wins: a newer request supersedes older ones mid-flight (agy aborted).
  - agy missing / failure → `error` reply (surfaced once in :NeocursorLog).

Launch:  uv run --with 'httpx[http2]' sidecar_agy.py   (same cmd as sidecar.py)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import difflib

# Pin the stdio codec (same reasoning as sidecar.py).
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", newline="\n", line_buffering=True
)
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

MARKER = "NEOCURSOR__MARK"
SETTLE_MS = 800          # min quiet time before a real agy request
AGY_TIMEOUT_S = 90       # agy headless with a big file can be slow
AGY = shutil.which("agy") or "agy"

wlock = threading.Lock()
latest = {"gen": 0}
last_run_at = {"ms": 0}
env = dict(os.environ)


def emit(obj):
    with wlock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def now_ms():
    return int(time.time() * 1000)


def build_temp_file(req):
    """Copy of the buffer with MARKER inserted at (line0, col0)."""
    lines = (req.get("content") or "").split("\n")
    line = req.get("line", 0)
    col = req.get("col", 0)
    if line < 0 or line > len(lines):
        line = min(max(line, 0), len(lines) - 1)
    row = lines[line] if line < len(lines) else ""
    col = max(0, min(col, len(row)))
    marked = row[:col] + MARKER + row[col:]
    marked_lines = lines[:line] + [marked] + lines[line + 1:]
    fd, tmp_path = tempfile.mkstemp(prefix="neocursor-agy-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(marked_lines))
    return tmp_path


def call_agy(req, workdir):
    """Run agy headless; return the produced file content or None."""
    if not req.get("path"):
        return None
    tmp_path = build_temp_file(req)
    try:
        prompt = (
            "Act as a tab-completion model. In the file "
            + tmp_path
            + ", replace the exact single marker '"
            + MARKER
            + "' with the code completion that naturally continues at that "
            + "exact cursor position in the surrounding code. Output ONLY the "
            + "full final file content with the marker gone. No commentary, "
            + "no markdown fences, no diff."
        )
        cmd = [AGY, "--print", "--add-dir", workdir, prompt]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=AGY_TIMEOUT_S, env=env
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout
        # strip a fenced code block if agy wraps it despite the instruction
        if out.count("```") >= 2:
            out = out.split("```", 2)[1]
        if MARKER in out:
            return None
        return out
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def neutral_edits(original, produced):
    """line diff original vs produced → [{text, range:{start1,endInclusive1}}]."""
    a = original.split("\n")
    b = (produced or "").split("\n")
    edits = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        text = "\n".join(b[j1:j2])
        if tag == "insert":
            # pure insert after 0-based line i1 → 1-based start=i1+1, end=i1
            edits.append(
                {"text": text, "range": {"start": i1 + 1, "endInclusive": i1}}
            )
        else:  # replace | delete → covers [i1..i2) 0-based
            edits.append(
                {"text": text, "range": {"start": i1 + 1, "endInclusive": i2}}
            )
    return edits


def serve(req, my_gen):
    rid = req.get("id")
    path = req.get("path") or "untitled"

    def cancelled():
        return latest["gen"] != my_gen

    # Settle guard: never spam agy inside a typing burst.
    with wlock:
        elapsed = now_ms() - last_run_at["ms"]
    if elapsed < SETTLE_MS:
        emit({"id": rid, "prediction": None, "text": "", "range": None, "edits": []})
        return
    with wlock:
        last_run_at["ms"] = now_ms()

    workdir = os.path.dirname(os.path.abspath(path)) if path and path != "untitled" else os.getcwd()
    produced = call_agy(req, workdir)
    if cancelled():
        emit({"id": rid, "aborted": True})
        return
    if produced is None:
        emit({"id": rid, "error": "agy host: no completion produced (lost mark / CLImissing?)"})
        return

    edits = neutral_edits(req.get("content") or "", produced)
    first = edits[0] if edits else {"text": "", "range": None}
    emit(
        {
            "id": rid,
            "text": first["text"],
            "range": first["range"],
            "edits": edits,
            "prediction": None,  # agy host: sin tab-to-jump cruzado por ahora
        }
    )


def main():
    sys.stderr.write("neocursor sidecar (host=antigravity) ready\n")
    sys.stderr.flush()
    emit(
        {
            "config": {
                # sin CppConfig: defaults sanos del motor; is_fused=true evita
                # la rama de "Tab indent on blank line" (sugerencias de agy no
                # son de modelo fused de Cursor)
                "debounce": 250,
                "exclude_patterns": [],
                "heuristics": [],
                "reject_hard": 2,
                "max_cleared": 20,
                "is_fused": True,
            }
        }
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            emit({"id": None, "error": str(e)})
            continue
        latest["gen"] += 1
        threading.Thread(target=serve, args=(req, latest["gen"]), daemon=True).start()


if __name__ == "__main__":
    main()