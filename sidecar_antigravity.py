#!/usr/bin/env python3
"""sidecar_antigravity.py — host=antigravity del cursortab (Ruta B).

El supercomplete de Antigravity es una LLM COMPLETION directa (descubierto por
MITM del LS) en:
  POST https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
  Auth: Bearer OAuth (refrescable via ~/.config/nvim/anty_oauth.json).
  Body: {"project":..., "request": {"contents":[{"role":"user","parts":[{"text":
        <archivo con <|cursor|>>}]}], "generationConfig":...}, "model":"tab_flash_lite_preview",
        "userAgent":"antigravity","requestType":"tab"}
  Respuesta SSE -> candidates[].content.parts[].text (continuación o archivo propuesto).

PROTOCOLO NEUTRO = sidecar.py (stdio JSON-lines {id,path,content,line,col} -> {id,edits,text,range}).
"""
import json
import os
import re
import sys
import difflib
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://daily-cloudcode-pa.googleapis.com"
OAUTH = os.path.expanduser("~/.config/nvim/anty_oauth.json")
TOKFILE = os.path.expanduser("~/.config/nvim/antigravity_token")
PROJECT = "aicode-consumers"
MODEL_TAB = "tab_flash_lite_preview"
MODEL_TAB_JUMP = "tab_jump_flash_lite_preview"


def refresh_bearer():
    if os.path.isfile(OAUTH):
        try:
            c = json.load(open(OAUTH))
            body = urllib.parse.urlencode({
                "refresh_token": urllib.parse.unquote(c.get("refresh_token") or ""),
                "client_id": c.get("client_id") or "",
                "client_secret": c.get("client_secret") or "",
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())["access_token"]
        except Exception:
            pass
    if os.path.isfile(TOKFILE):
        try:
            t = open(TOKFILE).read().strip().replace("Bearer ", "")
            if t.startswith("ya29"):
                return t
        except Exception:
            pass
    return None


def mark_cursor(content, line, col):
    lines = content.split("\n")
    row = max(0, min(line, len(lines) - 1))
    ln = lines[row] if row < len(lines) else ""
    cn = max(0, min(col, len(ln)))
    lines[row] = ln[:cn] + "<|cursor|>" + ln[cn:]
    return "\n".join(lines)


def tab_req(content, line, col, kind="tab"):
    payload = {
        "project": PROJECT,
        "requestId": f"neocursor/{kind}/" + os.urandom(8).hex(),
        "request": {
            "contents": [{"role": "user", "parts": [{"text": mark_cursor(content, line, col)}]}],
            "generationConfig": {"maxOutputTokens": 1500},
        },
        "model": MODEL_TAB,
        "userAgent": "antigravity",
        "requestType": "tab",
    }
    return payload


def call_tab(payload, bearer):
    h = {"Authorization": "Bearer " + bearer, "Content-Type": "application/json",
         "Accept": "text/event-stream",
         "User-Agent": "antigravity/2.1.1 linux/amd64 google-api-nodejs-client/10.3.0"}
    req = urllib.request.Request(BASE + "/v1internal:streamGenerateContent?alt=sse",
                                 data=json.dumps(payload).encode(), headers=h, method="POST")
    raw = urllib.request.urlopen(req, timeout=90).read().decode("utf-8", "replace")
    parts = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            j = json.loads(line[5:].strip())
            for c in j.get("response", {}).get("candidates", []):
                for p in c.get("content", {}).get("parts", []):
                    parts.append(p.get("text") or "")
        except Exception:
            continue
    return "".join(parts).strip()


def strip_fences(text):
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def neutral_edits(original, produced):
    a = original.split("\n")
    b = (produced or "").split("\n")
    edits = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        text = "\n".join(b[j1:j2])
        if tag == "insert":
            edits.append({"text": text, "range": {"start": i1 + 1, "endInclusive": i1}})
        else:
            edits.append({"text": text, "range": {"start": i1 + 1, "endInclusive": i2}})
    return edits


def serve():
    sys.stderr.write("neocursor sidecar (host=antigravity) ready\n")
    sys.stderr.flush()
    # Handshake de config PROACTIVO (igual que sidecar.py): el engine no lo
    # pide, lo recibe al boot. Heuristics vacías → motor con defaults sanos.
    sys.stdout.write(json.dumps({
        "config": {"debounce": 250, "exclude_patterns": [], "heuristics": [],
                   "reject_hard": 2, "max_cleared": 20, "is_fused": True}}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        if req.get("config") is not None:
            sys.stdout.write(json.dumps({
                "config": {"debounce": 250, "exclude_patterns": [], "heuristics": [],
                           "reject_hard": 2, "max_cleared": 20, "is_fused": True}}) + "\n")
            sys.stdout.flush()
            continue
        bearer = refresh_bearer()
        if not bearer:
            sys.stdout.write(json.dumps({"id": rid, "error": "token faltante (capture_anty_token.sh o anty_oauth.json)"}) + "\n")
            sys.stdout.flush()
            continue
        content = req.get("content") or ""
        try:
            payload = tab_req(content, req.get("line") or 0, req.get("col") or 0)
            text = call_tab(payload, bearer)
        except urllib.error.HTTPError as e:
            sys.stdout.write(json.dumps({"id": rid, "error": f"HTTP {e.code} {e.read().decode('utf-8','replace')[:200]}"}) + "\n")
            sys.stdout.flush()
            continue
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "error": str(e)}) + "\n")
            sys.stdout.flush()
            continue
        stripped = strip_fences(text)
        if not stripped:
            sys.stdout.write(json.dumps({"id": rid, "text": "", "range": None, "edits": [], "prediction": None}) + "\n")
            sys.stdout.flush()
            continue
        # Ghost (continuación en cursor) vs edición por diff del archivo completo
        edits = neutral_edits(content, stripped)
        if not edits:
            row1 = (req.get("line") or 0) + 1
            edits = [{"text": stripped, "range": {"start": row1, "endInclusive": row1}}]
        first = edits[0]
        sys.stdout.write(json.dumps({"id": rid, "text": first["text"], "range": first["range"],
                                     "edits": edits, "prediction": None}) + "\n")
        sys.stdout.flush()


serve()
