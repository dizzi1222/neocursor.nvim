#!/usr/bin/env python3
"""sidecar_antigravity.py — host=antigravity del cursortab (Ruta B).

El supercomplete de Antigravity es una LLM COMPLETION directa en:
  POST https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
  Auth: Bearer OAuth (refrescable via ~/.config/nvim/anty_oauth.json o token capturado).
  Modelo: tab_flash_lite_preview ('completion', no 'prediction').

POLÍTICA ANTI-BASURA (aprendida en pruebas):
  - Si se le manda el archivo ENTERO devuelve la solución completa reescrita
    (diffts gigantes que reemplazan/cuelgan el archivo). Por eso:
      1) se envía una VENTANA local alrededor del cursor (±WINDOW líneas),
      2) la línea del cursor se TRUNCA en la columna (clásico "completá desde acá"),
      3) del response SOLO se extrae el texto continuado justo después del cursor
         (ghost inline), descartando todo lo demás; si no se puede anclar → NOOP.
  Resultado: el Tab sugiere sólo la continuación local. NUNCA ediciones masivas.

PROTOCOLO NEUTRO (igual que sidecar.py): stdio JSON-lines.
  in : {"id","path","content","line","col","language"}
  out: {"id","text","range","edits","prediction"}   (edits máx. 1, ghost)
       {"id","error"}  /  {"config": {...}} al boot
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://daily-cloudcode-pa.googleapis.com"
OAUTH = os.path.expanduser("~/.config/nvim/anty_oauth.json")
TOKFILE = os.path.expanduser("~/.config/nvim/antigravity_token")
PROJECT = "aicode-consumers"
MODEL_TAB = "tab_flash_lite_preview"
WINDOW = int(os.environ.get("ANTY_WINDOW", "25"))  # líneas de contexto alrededor del cursor
GHOST_MAX_LINES = int(os.environ.get("ANTY_GHOST_MAX", "6"))


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


def local_window(content, line, col):
    """Slice cursor±WINDOW, línea del cursor TRUNCADA en col + marca."""
    lines = content.split("\n")
    lo = max(0, line - WINDOW)
    hi = min(len(lines), line + WINDOW + 1)
    win = lines[lo:hi]
    row = line - lo  # fila del cursor dentro de la ventana
    cur = win[row]
    win[row] = cur[:col] + "<|cursor|>"
    return "\n".join(win)


def tab_req(content, line, col):
    payload = {
        "project": PROJECT,
        "requestId": "neocursor/tab/" + os.urandom(8).hex(),
        "request": {
            "contents": [{"role": "user", "parts": [{"text": local_window(content, line, col)}]}],
            "generationConfig": {"maxOutputTokens": 400},
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
    for ln in raw.split("\n"):
        ln = ln.strip()
        if not ln.startswith("data:"):
            continue
        try:
            j = json.loads(ln[5:].strip())
            for c in j.get("response", {}).get("candidates", []):
                for p in c.get("content", {}).get("parts", []):
                    parts.append(p.get("text") or "")
        except Exception:
            continue
    return "".join(parts).strip()


def strip_fences(text):
    text = text.replace("<|cursor|>", "")
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def extract_ghost(content, line, col, resp):
    """Solo textto que continúa justo después del cursor. '' si no se puede anclar."""
    olines = content.split("\n")
    prefix = olines[min(line, len(olines) - 1)][:col]
    if not prefix:
        return ""
    rlines = resp.split("\n")
    idx = None
    for i, rl in enumerate(rlines):
        if rl.startswith(prefix):
            idx = i
            break
    if idx is None:
        return ""
    tail = rlines[idx][len(prefix):]
    added = []
    on = min(line + 1, len(olines) - 1)
    j = idx + 1
    while j < len(rlines):
        nj = rlines[j]
        if nj.startswith("```"):
            break
        if on < len(olines) and nj == olines[on]:
            break  # está re-echando el archivo original → cortar
        added.append(nj)
        j += 1
        on += 1
        if len(added) >= GHOST_MAX_LINES:
            break
    ghost_lines = ([tail] if tail else []) + added
    ghost = "\n".join(ghost_lines).strip()
    return ghost


def serve():
    sys.stderr.write("neocursor sidecar (host=antigravity) ready\n")
    sys.stderr.flush()
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
            continue  # config ya se envió proactivo
        bearer = refresh_bearer()
        if not bearer:
            sys.stdout.write(json.dumps({"id": rid, "error": "token faltante (capture_anty_token.sh o anty_oauth.json)"}) + "\n")
            sys.stdout.flush()
            continue
        content = req.get("content") or ""
        row0 = req.get("line") or 0
        col0 = req.get("col") or 0
        try:
            payload = tab_req(content, row0, col0)
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
        ghost = extract_ghost(content, row0, col0, stripped)
        if not ghost:
            sys.stdout.write(json.dumps({"id": rid, "text": "", "range": None, "edits": [], "prediction": None}) + "\n")
            sys.stdout.flush()
            continue
        row1 = row0 + 1  # 1-indexed inclusive start/end para insertar en la fila del cursor
        edit = {"text": ghost, "range": {"start": row1, "endInclusive": row0}}
        sys.stdout.write(json.dumps({"id": rid, "text": ghost, "range": edit["range"],
                                     "edits": [edit], "prediction": None}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve()