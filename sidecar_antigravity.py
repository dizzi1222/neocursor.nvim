#!/usr/bin/env python3
"""sidecar_antigravity.py — host=antigravity del cursortab.

El supercomplete de Antigravity es UNA LLAMADA LLM directa (descubierto por
MITM del LS): POST https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
Auth: Bearer OAuth (refrescable con ~/.config/nvim/anty_oauth.json).
Body: contents[] (archivo + cursor <|cursor|> + consigna de "next logical edit")
+ systemInstruction (prompt agente) + model=tab_flash_lite_preview (tab) /
tab_jump_flash_lite_preview (jump) + requestType.
Respuesta SSE -> candidates[].content.parts[].text con ReplacementChunks (edición).

Protocolo neutro = sidecar.py (stdio JSON-lines {id,path,content,line,col} -> {id,edits,text,range}).
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
MODEL_TAB_JUMP = "tab_jump_flash_lite_preview"
AGENT_SYSTEM = (
    "You are Antigravity Agent. The user will send you a code block whose next logical "
    "edit (a code completion continuing at the <|cursor|> marker) you must produce. "
    "Reply ONLY with the exact text that should appear at the cursor, nothing else."
)


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


def build_contents(path, content, line, col):
    lines = content.split("\n")
    row = max(0, min(line, len(lines) - 1))
    ln = lines[row] if row < len(lines) else ""
    cn = max(0, min(col, len(ln)))
    marked = lines[:row] + [ln[:cn] + "<|cursor|>" + ln[cn:]] + lines[row + 1:]
    file_text = "\n".join(marked)
    return [
        {"role": "user", "parts": [{"text": f"File Path: `file://{path}`\n{file_text}"}]},
        {"role": "user", "parts": [{"text": (
            "<USER_REQUEST>\nPlease modify the following mentioned code block with "
            "the logical next edit. The <|cursor|> in the code block represents "
            "where my cursor is.\n</USER_REQUEST>\n"
            f"Cursor is on line: {line + 1} column: {col}\n"
        )}]},
    ]


def tab_req(path, content, line, col, kind="tab"):
    payload = {
        "project": PROJECT,
        "requestId": f"neocursor/{kind}/auto",
        "request": {
            "contents": build_contents(path, content, line, col),
            "systemInstruction": {"role": "user", "parts": [{"text": AGENT_SYSTEM}]},
            "generationConfig": {"maxOutputTokens": 1024,
                                 "thinkingConfig": {"includeThoughts": False, "thinkingBudget": 0}},
        },
        "model": MODEL_TAB_JUMP if kind == "jump" else MODEL_TAB,
        "userAgent": "antigravity",
        "requestType": kind,
    }
    return payload


def call_tab(payload, bearer):
    h = {"Authorization": "Bearer " + bearer, "Content-Type": "application/json",
         "Accept": "text/event-stream", "User-Agent": "antigravity/2.1.1 linux/amd64 google-api-nodejs-client/10.3.0"}
    req = urllib.request.Request(BASE + "/v1internal:streamGenerateContent?alt=sse",
                                 data=json.dumps(payload).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", "replace")
    text = ""
    for m in re.finditer(r'data:\s*(\{.*\})', raw, re.S):
        try:
            d = json.loads(m.group(1))
            cands = ((d.get("response") or {}).get("candidates")) or []
            for c in cands:
                for p in (c.get("content") or {}).get("parts") or []:
                    text += p.get("text") or ""
        except Exception:
            continue
    return text.strip()


def render_edits(text, line0):
    if not text:
        return []
    block = re.search(r'<replace_file_content>(.*?)</replace_file_content>', text, re.S |
                      re.I)
    edits = []
    if block:
        js = re.search(r'\{.*\}', block.group(1), re.S)
        if js:
            try:
                data = json.loads(js.group(0))
                for ch in data.get("ReplacementChunks") or []:
                    edits.append({"text": ch.get("ReplacementContent") or "",
                                  "target": ch.get("TargetContent") or ""})
            except Exception:
                edits = []
    if edits:
        return edits
    return [{"text": text, "target": None}]  # ghost: insertar en cursor


def serve():
    buf = ""
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
        try:
            payload = tab_req(req.get("path") or "untitled", req.get("content") or "",
                              req.get("line") or 0, req.get("col") or 0)
            text = call_tab(payload, bearer)
        except urllib.error.HTTPError as e:
            sys.stdout.write(json.dumps({"id": rid, "error": f"HTTP {e.code} {e.read().decode('utf-8','replace')[:200]}"}) + "\n")
            sys.stdout.flush()
            continue
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "error": str(e)}) + "\n")
            sys.stdout.flush()
            continue
        edits = render_edits(text, req.get("line") or 0)
        out = []
        for e in edits:
            if e.get("target"):
                pass  # buscamos el target en content para el rango (futuro)
            rng = {"start": (req.get("line") or 0) + 1, "endInclusive": (req.get("line") or 0) + 1}
            out.append({"text": e["text"], "range": rng})
        first = out[0] if out else {"text": "", "range": None}
        sys.stdout.write(json.dumps({"id": rid, "text": first["text"], "range": first["range"],
                                     "edits": out, "prediction": None}) + "\n")
        sys.stdout.flush()


serve()