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
import difflib
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
WINDOW = int(os.environ.get("ANTY_WINDOW", "16"))
DOWN = int(os.environ.get("ANTY_DOWN", "16"))
FILE_MAX_LINES = int(os.environ.get("ANTY_FILE_MAX", "200"))
MAX_REPLACE_LINES = int(os.environ.get("ANTY_MAX_REPLACE", "28"))
MAX_REPLACE_CURSOR_DIST = int(os.environ.get("ANTY_MAX_CURSOR_DIST", "5"))
GHOST_MAX_LINES = int(os.environ.get("ANTY_GHOST_MAX", "3"))
GHOST_MAX_CHARS = int(os.environ.get("ANTY_GHOST_MAX_CHARS", "400"))
GHOST_NEAR = int(os.environ.get("ANTY_GHOST_NEAR", "6"))

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


def local_window(content, line, col, lang=""):
    lines = content.split("\n")
    if len(lines) <= FILE_MAX_LINES:
        # Archivo corto → mandarlo COMPLETO con <|cursor|> insertado en la
        # línea, sin borrar lo que sigue. El modelo ve el archivo real y
        # completa sin inventar borrados.
        lo, hi = 0, len(lines)
    else:
        lo = max(0, line - WINDOW)
        hi = min(len(lines), line + 1 + DOWN)
    win = lines[lo:hi]
    row = min(line - lo, len(win) - 1)
    cur = win[row]
    # con `<|cursor|>` dentro, el resto de la línea original queda fuera:
    # si el cursor va seguido de texto visible, se preserva como contexto
    if col < len(cur):
        win[row] = cur[:col] + "<|cursor|>" + cur[col:]
    else:
        win[row] = cur[:col] + "<|cursor|>"
    head = ""
    if lang:
        head = f"This is a {lang} file.\n\n"
    return head + "\n".join(win)


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


def lang_for_path(path):
    if not path:
        return ""
    ext = os.path.splitext(path)[1].lower()
    return {
        ".ts": "TypeScript", ".tsx": "TypeScript/React",
        ".js": "JavaScript", ".jsx": "JavaScript/React",
        ".py": "Python", ".rs": "Rust", ".go": "Go",
        ".java": "Java", ".c": "C", ".h": "C", ".cpp": "C++",
        ".hpp": "C++", ".cs": "C#", ".rb": "Ruby", ".php": "PHP",
        ".swift": "Swift", ".kt": "Kotlin", ".lua": "Lua",
        ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
        ".css": "CSS", ".scss": "SCSS", ".html": "HTML",
        ".json": "JSON", ".md": "Markdown",
    }.get(ext, "")


def tab_req(content, line, col, path=""):
    payload = {
        "project": PROJECT,
        "requestId": "neocursor/tab/" + os.urandom(8).hex(),
        "request": {
            "contents": [{"role": "user", "parts": [{
                "text": local_window(content, line, col, lang_for_path(path))
            }]}],
            "systemInstruction": {
                "role": "user",
                "parts": [{
                    "text": (
                        "You are an expert software engineer working inside an "
                        "editor. Given the file context below, write the complete "
                        "file content including your edit after <|cursor|>."
                    )
                }],
            },
            "sessionId": SESSION_ID,
            "generationConfig": {"maxOutputTokens": 256,
                                 "thinkingConfig": {"includeThoughts": False}},
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
    return calls, _unescape_text("".join(texts)).strip()


def _unescape_text(text):
    """El modelo a veces mezcla el gold con escapes JSON literales (\\n, \\\"
    como 2 chars) entre newlines reales. Decodificar solo esas secuencias de
    forma determinista — json.loads es frágil (revienta con comillas reales)."""
    if "\\n" not in text and '\\"' not in text and "\\t" not in text and "\\\\" not in text:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "t":
                out.append("\t"); i += 2; continue
            if nxt == "r":
                out.append("\r"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
            if nxt == '"':
                out.append('"'); i += 2; continue
        out.append(ch)
        i += 1
    return "".join(out)


# ----------------------------------------------------------------------------
def one_edit(buffer_lines, s1, e1, tgt, rep):
    if not (1 <= s1 <= e1 <= len(buffer_lines)):
        return None
    if "\n".join(buffer_lines[s1 - 1 : e1]) != tgt:
        return None
    return {"text": rep, "range": {"start": s1, "endInclusive": e1}}


def scope_ok(n_lines, s1, e1, cursor_line, full_context):
    """Valida el rango de un tool-call de reemplazo.

    El cap de líneas (MAX_REPLACE_LINES) SIEMPRE aplica. La proximidad al
    cursor (MAX_REPLACE_CURSOR_DIST) solo es filtro DURO cuando el modelo NO
    vio el archivo completo (archivo grande → ventana 16+16): en ese caso un
    edit lejano sería un ancla a ciegas (suerte). Con el archivo completo
    visible, la proximidad NO bloquea — solo ordena las sugerencias (el
    'siguiente edit' puede estar en cualquier parte del archivo)."""
    if not (1 <= s1 <= e1 <= n_lines):
        return False
    if (e1 - s1 + 1) > MAX_REPLACE_LINES:
        return False
    if not full_context and abs(s1 - (cursor_line + 1)) > MAX_REPLACE_CURSOR_DIST:
        return False
    return True


def is_duplicate_block(buffer_lines, s1, e1, lines):
    """True si `lines` (≥1) ya aparece completo y contiguo en el buffer FUERA
    del rango [s1,e1]. Ataca el caso 'el modelo re-propone el bloque que el
    usuario ya escribió' (pisa código reciente, lo duplica arriba/abajo)."""
    if not lines or not lines[0].strip():
        return False
    n = len(lines)
    text = lines[:n]
    for i, ln in enumerate(buffer_lines):
        if s1 - 1 <= i <= e1 - 1:
            continue
        if i + n > len(buffer_lines):
            break
        if buffer_lines[i : i + n] == text:
            return True
    return False


def function_call_edits(buffer_lines, name, args, cursor_line, full_context):
    edits = []
    n_lines = len(buffer_lines)
    try:
        if name == "replace_file_content":
            s1, e1 = int(args["StartLine"]), int(args["EndLine"])
            if scope_ok(n_lines, s1, e1, cursor_line, full_context):
                e = one_edit(buffer_lines, s1, e1,
                             args["TargetContent"], args["ReplacementContent"])
                if e:
                    edits.append(e)
        elif name == "multi_replace_file_content":
            for ch in args.get("ReplacementChunks") or []:
                s1, e1 = int(ch["StartLine"]), int(ch["EndLine"])
                if scope_ok(n_lines, s1, e1, cursor_line, full_context):
                    e = one_edit(buffer_lines, s1, e1,
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


def xml_tool_edits(buffer_lines, text, cursor_line, full_context):
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
                if scope_ok(len(buffer_lines), s1, e1, cursor_line, full_context):
                    e = one_edit(buffer_lines, s1, e1, tgt, rep)
                    if e:
                        edits.append(e)
        else:
            s1 = to_int(fields.get("StartLine"))
            e1 = to_int(fields.get("EndLine"))
            if scope_ok(len(buffer_lines), s1, e1, cursor_line, full_context):
                e = one_edit(buffer_lines, s1, e1,
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
    # recortar colas de tool-call a medio emitir (residuos XML/JSON)
    for marker in ("</replace_file_content>", "</multi_replace_file_content>", "<replace_file_content>", "<multi_replace_file_content>"):
        if marker in text:
            text = text.split(marker)[0]
    # cortar loops de comillas escapadas tipo "\n"\n"\n...
    m = re.search(r'(?:"\n"){2,}', text)
    if m:
        text = text[: m.start()]
    # residuo de tool-call JSON a medio emitir: comilla + newline + tab (}"\n\t*}]
    m = re.search(r'["\']\n\t', text)
    if m and m.start() > 0:
        text = text[: m.start()]
    text = re.sub(r"[\"']\s*\n\t*[}\]]\s*$", "", text)
    # cola JSON huérfana: comilla solitaria al final (artefacto del escape)
    text = re.sub(r'["\']\s*$', "", text)
    return text.strip()


def diff_edits(content, line, resp):
    """Omnipresencia: difflib del gold contra el buffer para localizar el edit
    en su posición REAL (no solo en el cursor). Devuelve [{text,range}] con
    range 1-indexado inclusive, o [] si no hay un hunk coherente post-cursor
    con anchor.

    Confianza: exige que el gold esté replicando el MISMO archivo (coincidencia
    global alta) — si el modelo inventó un archivo distinto (ej. cambió una
    interface por otra), el diff no es confiable y se descarta."""
    olines = content.split("\n")
    rlines = resp.split("\n")
    if not rlines:
        return []
    sm = difflib.SequenceMatcher(None, olines, rlines, autojunk=False)
    equal_n = sum((j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op == "equal")
    min_n = min(len(olines), len(rlines))
    # si el gold replica <60% de las líneas, está inventando otro archivo
    if min_n and (equal_n / min_n) < 0.60:
        return []

    edits = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        old = olines[i1:i2]
        new = rlines[j1:j2]
        if op == "delete":
            # borrado puro: el modelo quitó líneas → reemplazar el rango por nada
            if (i2 - i1) > GHOST_MAX_LINES + 5:  # hasta un bloque (~8 ln)
                continue
            edits.append({
                "text": "",
                "range": {"start": i1 + 1, "endInclusive": i2},
                "old": old,
            })
            continue
        if not new:
            continue
        # solo reemplazos/inserciones, no deletes puros
        text = "\n".join(new).strip()
        if not text or len(text) > GHOST_MAX_CHARS:
            continue
        if (j2 - j1) > GHOST_MAX_LINES + 2:
            continue
        edits.append({
            "text": text,
            "range": {"start": i1 + 1, "endInclusive": i2},  # 1-indexed inclusive
            "old": old,
        })
    if not edits:
        return []
    edits.sort(key=lambda e: abs(e["range"]["start"] - (line + 1)))
    return edits[:1]


def extract_ghost(content, line, col, resp):
    if _ESCAPE.search(resp):
        return ""
    olines = content.split("\n")
    prefix = olines[min(line, len(olines) - 1)][:col]
    if not prefix:
        return ""
    rlines = resp.split("\n")

    # Candidatos: TODAS las líneas del gold que empiecen con el prefix del
    # cursor (el gold replica el archivo, así que puede haber varias). Elegir
    # la más CERCANA a la posición del cursor, no la primera global — este era
    # el bug que insertaba basura de otra parte del archivo.
    cands = [i for i, rl in enumerate(rlines) if rl.startswith(prefix)]
    if not cands:
        # continuación pura: tail corto (≤5 ln) sin top-level/import → no es
        # otro archivo. Un gold de otro buffer (React, etc.) arranca con
        # import/export/top-level → descartar.
        if len(rlines) > GHOST_MAX_LINES + 2:
            return ""
        first = rlines[0].strip() if rlines else ""
        if not first or first.startswith("import") or _TOPLEVEL.match(first):
            return ""
        ghost = "\n".join(rlines).strip()
        if not ghost or len(ghost) > GHOST_MAX_CHARS:
            return ""
        return ghost

    # trozo sin prefijo: un ghost que arranca pegando al cursor

    def anchor_ok(idx):
        # ≥2 líneas antes del candidato deben coincidir con el buffer previo al
        # cursor (coherencia: gold replicando ESTA zona). En las primeras 2
        # líneas (sin 2 previas), basta 1 coincidencia.
        need = 1 if line < 2 else 2
        k = 0
        while k < need and line - 1 - k >= 0 and idx - 1 - k >= 0 and olines[line - 1 - k] == rlines[idx - 1 - k]:
            k += 1
        return k >= need

    def score(idx):
        dist = abs(idx - min(line, len(rlines) - 1))
        return dist

    anchored = [i for i in cands if anchor_ok(i)]
    # fiabilidad TOTAL: solo candidatos con anchor — si el gold no replica
    # coherentemente la zona del cursor (otro archivo, otro bloque), no sugerir.
    # (La opción "max" aceptaba el más cercano sin anchor y permitía golds
    # incoherentes que rompían el buffer — confirmado en vivo.)
    if not anchored:
        return ""
    idx = min(anchored, key=score)

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
        "config": {"debounce": 400, "exclude_patterns": [], "heuristics": [
            "HEURISTIC_DUPLICATING_LINE_AFTER_SUGGESTION",
            "HEURISTIC_REVERTING_USER_CHANGE",
        ],
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
        path = req.get("path") or ""
        try:
            payload = tab_req(content, row0, col0, path)
            calls, text = call_tab(payload, bearer)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # token vencido/invalidado → re-captura automática + 1 reintento
                new_tok = auto_refresh_token()
                if new_tok:
                    try:
                        payload = tab_req(content, row0, col0, path)
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
        full_context = len(buffer_lines) <= FILE_MAX_LINES
        edits = []
        for fc in calls:
            edits.extend(function_call_edits(buffer_lines, fc.get("name"), fc.get("args") or {}, row0, full_context))
        if not edits:
            edits = xml_tool_edits(buffer_lines, text, row0, full_context)
        if edits:
            # los edits de tool-call llegan en orden arbitrario del modelo:
            # ordenar por cercanía al cursor (factor relevante, no definitivo)
            edits.sort(key=lambda e: abs(e["range"]["start"] - (row0 + 1)))
            # cap defensivo: ≤6 edits, cada reemplazo ≤ MAX_REPLACE_LINES
            edits = [e for e in edits if (e["range"]["endInclusive"] - e["range"]["start"] + 1) <= MAX_REPLACE_LINES][:6]
            # filtro anti-duplicado: descartar edits que repiten un bloque existente
            edits = [
                e for e in edits
                if not is_duplicate_block(
                    buffer_lines, e["range"]["start"], e["range"]["endInclusive"],
                    e["text"].split("\n"))
            ]
        if edits:
            first = edits[0]
            sys.stdout.write(json.dumps({"id": rid, "text": first["text"], "range": first["range"],
                                         "edits": edits, "prediction": None}) + "\n")
            sys.stdout.flush()
            continue
        # omnipresencia: si el gold replica fielmente el archivo, difflib ubica
        # el "siguiente edit" en su posición real (no solo en el cursor) → el
        # lado Lua puede saltar ahí (tab-tab-tab). Solo si el gold es coherente.
        stripped = strip_fences(text)
        d_edits = diff_edits(content, row0, stripped)
        if d_edits and not is_duplicate_block(
                buffer_lines, d_edits[0]["range"]["start"], d_edits[0]["range"]["endInclusive"],
                d_edits[0]["text"].split("\n")):
            # descartar no-op: el texto nuevo igual al viejo (diff en vano)
            if "\n".join(d_edits[0].get("old") or []) != d_edits[0]["text"]:
                first = d_edits[0]
                pred = None
                # si el edit está FUERA de la línea del cursor, prediction → jump
                if first["range"]["start"] != row0 + 1:
                    pred = {"path": req.get("path") or "", "line": first["range"]["start"]}
                sys.stdout.write(json.dumps({"id": rid, "text": first["text"],
                                             "range": first["range"],
                                             "edits": [first], "prediction": pred}) + "\n")
                sys.stdout.flush()
                continue
        # fallback ghost
        ghost = extract_ghost(content, row0, col0, stripped)
        if ghost and is_duplicate_block(buffer_lines, row0 + 1, row0, ghost.split("\n")):
            ghost = ""
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