#!/usr/bin/env python3
"""sidecar_antigravity.py — host=antigravity del cursortab (Ruta B, v2 estructurado).

END POINT: POST https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
  Auth: Bearer OAuth (anty_oauth.json refresh + fallback antigravity_token).
  Request (familia verídica del agente capturada): tools[
    replace_file_content + multi_replace_file_content ], toolConfig mode=VALIDATED,
    sessionId fijo, system corto "solo tool-call", window local con <|cursor|>.
  Respuesta SSE -> candidates[].content.parts[].functionCall{.name,.args} (JSON) o
    texto con bloque XML <replace_file_content>/<multi_replace_file_content>.

POLÍTICA (anti-basura + anti-destructivo):
  1) Si llega functionCall JSON válido (TargetContent EXACTO en el buffer) → edits
     con rangos explícitos (multi-hunk → chain/jump con el motor).
  2) Si llega XML de tool-call best-effort parseable → edits.
  3) Si el modelo devuelve texto plano → GHOST seguro (extract_ghost + guards).
  4) JAMÁS se emiten reemplazos que no calcen TargetContent exacto; NOOP cuando
     no se puede anclar o hay escapes (\\n / \\" literales).

PROTOCOLO NEUTRO (igual que sidecar.py): stdio JSON-lines.
  in : {"id","path","content","line","col","language"}
  out: {"id","text","range","edits","prediction"}   (edits 0..N)
       {"id","error"}  /  {"config": {...}} al boot
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://daily-cloudcode-pa.googleapis.com"
OAUTH = os.path.expanduser("~/.config/nvim/anty_oauth.json")
TOKFILE = os.path.expanduser("~/.config/nvim/antigravity_token")
PROJECT = "aicode-consumers"
MODEL_TAB = "tab_flash_lite_preview"
SESSION_ID = os.environ.get("ANTY_SESSION", "-3750763034362895579")
WINDOW = int(os.environ.get("ANTY_WINDOW", "25"))
GHOST_MAX_LINES = int(os.environ.get("ANTY_GHOST_MAX", "3"))
GHOST_MAX_CHARS = int(os.environ.get("ANTY_GHOST_MAX_CHARS", "200"))
_HAS_TOKEN_OVERRIDE = bool(os.environ.get("ANTY_TOKEN"))

_TOPLEVEL = re.compile(r"^(export|import|function|class|interface|type|const|let|var|enum|namespace|pub|fn|def)\b")
_ESCAPE = re.compile(r'\\[nrtu0]|\\"|\\\\')


# ----------------------------------------------------------------------------
def refresh_bearer():
    if os.environ.get("ANTY_TOKEN"):
        return os.environ["ANTY_TOKEN"].strip()
    if os.path.isfile(TOKFILE):
        try:
            t = open(TOKFILE).read().strip().replace("Bearer ", "")
            if t.startswith("ya29"):
                return t
        except Exception:
            pass
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
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())["access_token"]
        except Exception:
            pass
    return None


def local_window(content, line, col):
    lines = content.split("\n")
    lo = max(0, line - WINDOW)
    hi = min(len(lines), line + WINDOW + 1)
    win = lines[lo:hi]
    row = line - lo
    cur = win[row]
    win[row] = cur[:col] + "<|cursor|>"
    return "\n".join(win)


def tools_decl():
    return [
        {
            "functionDeclarations": [
                {
                    "name": "replace_file_content",
                    "description": (
                        "Edit a file replacing a SINGLE CONTIGUOUS block. StartLine / "
                        "EndLine (1-indexed, StartLine <= EndLine) delimit a range whose "
                        "text EXACTLY equals TargetContent; ReplacementContent is the "
                        "drop-in replacement."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "TargetFile": {"type": "STRING"},
                            "Instruction": {"type": "STRING"},
                            "Description": {"type": "STRING"},
                            "AllowMultiple": {"type": "BOOLEAN"},
                            "TargetContent": {"type": "STRING"},
                            "ReplacementContent": {"type": "STRING"},
                            "StartLine": {"type": "INTEGER"},
                            "EndLine": {"type": "INTEGER"},
                            "toolSummary": {"type": "STRING"},
                            "toolAction": {"type": "STRING"},
                        },
                        "required": [
                            "TargetFile", "Instruction", "Description", "AllowMultiple",
                            "TargetContent", "ReplacementContent",
                            "StartLine", "EndLine", "toolSummary", "toolAction",
                        ],
                    },
                },
                {
                    "name": "multi_replace_file_content",
                    "description": (
                        "Edit a file with MULTIPLE NON-CONTIGUOUS blocks. Each entry of "
                        "ReplacementChunks is a ReplacementChunk with StartLine, EndLine, "
                        "TargetContent (exact) and ReplacementContent."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "TargetFile": {"type": "STRING"},
                            "Instruction": {"type": "STRING"},
                            "Description": {"type": "STRING"},
                            "ReplacementChunks": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "StartLine": {"type": "INTEGER"},
                                        "EndLine": {"type": "INTEGER"},
                                        "TargetContent": {"type": "STRING"},
                                        "ReplacementContent": {"type": "STRING"},
                                    },
                                    "required": [
                                        "StartLine", "EndLine",
                                        "TargetContent", "ReplacementContent",
                                    ],
                                },
                            },
                            "toolSummary": {"type": "STRING"},
                            "toolAction": {"type": "STRING"},
                        },
                        "required": ["TargetFile", "Instruction", "Description",
                                     "ReplacementChunks", "toolSummary", "toolAction"],
                    },
                },
            ]
        }
    ]


def tab_req(content, line, col):
    payload = {
        "project": PROJECT,
        "requestId": "neocursor/tab/" + os.urandom(8).hex(),
        "request": {
            "contents": [{"role": "user", "parts": [{
                "text": (
                    "The cursor is at <|cursor|>. Make the NEXT LOGICAL EDIT at the "
                    "cursor: output a replace_file_content (one block) or "
                    "multi_replace_file_content (several blocks) tool call whose "
                    "TargetContent EXACTLY matches the file text. Never return plain "
                    "text file rewrites.\n\n" + local_window(content, line, col)
                )
            }]}],
            "systemInstruction": {
                "role": "user",
                "parts": [{
                    "text": (
                        "You are a completion engine inside an editor. Your ONLY "
                        "allowed output is a replace_file_content or "
                        "multi_replace_file_content tool call for the next logical edit "
                        "at <|cursor|>. TargetContent must match the file verbatim."
                    )
                }],
            },
            "tools": tools_decl(),
            "toolConfig": {"functionCallingConfig": {"mode": "VALIDATED"}},
            "sessionId": SESSION_ID,
            "generationConfig": {"maxOutputTokens": 900},
            "labels": {"model_enum": "MODEL_PLACEHOLDER_M71"},
        },
        "model": MODEL_TAB,
        "userAgent": "antigravity",
        "requestType": "tab",
    }
    return payload


def call_tab(payload, bearer):
    h = {
        "Authorization": "Bearer " + bearer,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "antigravity/2.1.1 linux/amd64 google-api-nodejs-client/10.3.0",
    }
    req = urllib.request.Request(
        BASE + "/v1internal:streamGenerateContent?alt=sse",
        data=json.dumps(payload).encode(), headers=h, method="POST",
    )
    raw = urllib.request.urlopen(req, timeout=90).read().decode("utf-8", "replace")
    calls, texts = [], []
    for ln in raw.split("\n"):
        ln = ln.strip()
        if not ln.startswith("data:"):
            continue
        try:
            j = json.loads(ln[5:].strip())
        except Exception:
            continue
        for c in j.get("response", {}).get("candidates", []):
            for p in c.get("content", {}).get("parts", []):
                if p.get("functionCall"):
                    calls.append(p["functionCall"])
                elif p.get("text"):
                    texts.append(p["text"])
    return calls, "".join(texts).strip()


# ----------------------------------------------------------------------------
def one_edit(buffer_lines, s1, e1, tgt, rep):
    if not (1 <= s1 <= e1 <= len(buffer_lines)):
        return None
    if "\n".join(buffer_lines[s1 - 1 : e1]) != tgt:
        return None
    return {"text": rep, "range": {"start": s1, "endInclusive": e1}}


def function_call_edits(buffer_lines, name, args):
    edits = []
    try:
        if name == "replace_file_content":
            e = one_edit(buffer_lines, int(args["StartLine"]), int(args["EndLine"]),
                         args["TargetContent"], args["ReplacementContent"])
            if e:
                edits.append(e)
        elif name == "multi_replace_file_content":
            for ch in args.get("ReplacementChunks") or []:
                e = one_edit(buffer_lines, int(ch["StartLine"]), int(ch["EndLine"]),
                             ch["TargetContent"], ch["ReplacementContent"])
                if e:
                    edits.append(e)
    except (KeyError, TypeError, ValueError):
        pass
    return edits


_XML_RE = re.compile(
    r"<\s*(multi_?replace_file_content|replace_file_content)[^>]*>" + r"([\s\S]*?)" + r"</\s*\1\s*>",
    re.I,
)
_TAG_FIELD = re.compile(r"<\s*([A-Za-z0-9_]+)\s*>([\s\S]*?)</\s*\1\s*>")


def xml_tool_edits(buffer_lines, text):
    """Best-effort: parsea bloques XML de tool-call a edits."""
    edits = []
    if _ESCAPE.search(text):
        return []
    def clean(v):
        # preserva indentación de la izquierda; quita solo espacios finales
        return "\n".join(l.rstrip() for l in v.split("\n"))
    for m in _XML_RE.finditer(text):
        body = m.group(2)
        fields = {}
        for fm in _TAG_FIELD.finditer(body):
            fields[fm.group(1)] = fm.group(2)
        chunks = []
        for cm in re.finditer(r"<ReplacementChunk>([\s\S]*?)</ReplacementChunk>", body, re.I):
            cf = {}
            for fm in _TAG_FIELD.finditer(cm.group(1)):
                cf[fm.group(1)] = fm.group(2)
            chunks.append(cf)
        def to_int(v, d=0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        if chunks:
            for ch in chunks:
                s1 = to_int(ch.get("StartLine"))
                e1 = to_int(ch.get("EndLine"))
                tgt = clean(ch.get("TargetContent") or "")
                rep = clean(ch.get("ReplacementContent") or "")
                e = one_edit(buffer_lines, s1, e1, tgt, rep)
                if e:
                    edits.append(e)
        else:
            e = one_edit(buffer_lines, to_int(fields.get("StartLine")),
                         to_int(fields.get("EndLine")),
                         clean(fields.get("TargetContent") or ""),
                         clean(fields.get("ReplacementContent") or ""))
            if e:
                edits.append(e)
    return edits


def strip_fences(text):
    text = text.replace("<|cursor|>", "")
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def extract_ghost(content, line, col, resp):
    if _ESCAPE.search(resp):
        return ""
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
            break
        if _TOPLEVEL.match(nj.lstrip()):
            break
        added.append(nj)
        j += 1
        on += 1
        if len(added) >= GHOST_MAX_LINES:
            break
    ghost = "\n".join(([tail] if tail else []) + added).strip()
    if not ghost or len(ghost) > GHOST_MAX_CHARS:
        return ""
    return ghost


# ----------------------------------------------------------------------------
_last_refresh_attempt = {"at": 0.0}
REFRESH_COOLDOWN_S = 120  # ≥2 min entre recapturas automáticas


def auto_refresh_token():
    """Re-captura el Bearer corriendo capture_anty_token.sh (MITM 1-shot)."""
    now = time.time()
    if now - _last_refresh_attempt["at"] < REFRESH_COOLDOWN_S:
        return None
    _last_refresh_attempt["at"] = now
    script = os.path.expanduser("~/.config/nvim/capture_anty_token.sh")
    if not os.path.isfile(script):
        return None
    try:
        subprocess.run([script], timeout=150, capture_output=True)
    except Exception:
        return None
    if os.path.isfile(TOKFILE):
        try:
            t = open(TOKFILE).read().strip().replace("Bearer ", "")
            if t.startswith("ya29"):
                sys.stderr.write("neocursor: token re-capturado automáticamente\n")
                sys.stderr.flush()
                return t
        except Exception:
            pass
    return None


def serve():
    sys.stderr.write("neocursor sidecar (host=antigravity) ready\n")
    sys.stderr.flush()
    sys.stdout.write(json.dumps({
        "config": {"debounce": 400, "exclude_patterns": [], "heuristics": [],
                   "reject_hard": 2, "max_cleared": 20, "is_fused": True}}) + "\n")
    sys.stdout.flush()
    for ln in sys.stdin:
        ln = ln.strip()
        if not ln:
            continue
        try:
            req = json.loads(ln)
        except Exception:
            continue
        rid = req.get("id")
        if req.get("config") is not None:
            continue
        bearer = refresh_bearer()
        if not bearer:
            sys.stdout.write(json.dumps({"id": rid, "error": "token faltante (capture_anty_token.sh)"}) + "\n")
            sys.stdout.flush()
            continue
        content = req.get("content") or ""
        row0 = req.get("line") or 0
        col0 = req.get("col") or 0
        try:
            payload = tab_req(content, row0, col0)
            calls, text = call_tab(payload, bearer)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # token vencido/invalidado → re-captura automática + 1 reintento
                new_tok = auto_refresh_token()
                if new_tok:
                    try:
                        payload = tab_req(content, row0, col0)
                        calls, text = call_tab(payload, new_tok)
                        bearer = new_tok
                    except urllib.error.HTTPError as e2:
                        sys.stdout.write(json.dumps({"id": rid, "error": f"HTTP {e2.code} (post-refresh)"}) + "\n")
                        sys.stdout.flush()
                        continue
                    except Exception as ex:
                        sys.stdout.write(json.dumps({"id": rid, "error": str(ex)[:200]}) + "\n")
                        sys.stdout.flush()
                        continue
                else:
                    sys.stdout.write(json.dumps({"id": rid, "error": "HTTP 401 (re-captura fallida o en cooldown)"}) + "\n")
                    sys.stdout.flush()
                    continue
            else:
                sys.stdout.write(json.dumps({"id": rid, "error": f"HTTP {e.code}"}) + "\n")
                sys.stdout.flush()
                continue
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "error": str(e)[:200]}) + "\n")
            sys.stdout.flush()
            continue

        buffer_lines = content.split("\n")
        edits = []
        for fc in calls:
            edits.extend(function_call_edits(buffer_lines, fc.get("name"), fc.get("args") or {}))
        if not edits:
            edits = xml_tool_edits(buffer_lines, text)
        if edits:
            # cap defensivo: ≤6 edits, cada reemplazo ≤40 líneas
            edits = [e for e in edits if (e["range"]["endInclusive"] - e["range"]["start"] + 1) <= 40][:6]
        if edits:
            first = edits[0]
            sys.stdout.write(json.dumps({"id": rid, "text": first["text"], "range": first["range"],
                                         "edits": edits, "prediction": None}) + "\n")
            sys.stdout.flush()
            continue
        # fallback ghost
        stripped = strip_fences(text)
        ghost = extract_ghost(content, row0, col0, stripped)
        if not ghost:
            sys.stdout.write(json.dumps({"id": rid, "text": "", "range": None, "edits": [], "prediction": None}) + "\n")
            sys.stdout.flush()
            continue
        edit = {"text": ghost, "range": {"start": row0 + 1, "endInclusive": row0}}
        sys.stdout.write(json.dumps({"id": rid, "text": ghost, "range": edit["range"],
                                     "edits": [edit], "prediction": None}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve()