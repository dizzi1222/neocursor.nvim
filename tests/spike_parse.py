#!/usr/bin/env python3
"""SPIKE F2 — request IDE-like + parse de functionCall a edits.

Toma las declaraciones de tools capturadas (fixtures) y arma el request como
lo hace el agente/IDE: SOLO replace_file_content + multi_replace_file_content
registrados, systemInstruction corto "solo tool-call", window local con
<|cursor|>. Parsea parts[].functionCall.args -> edits[{text, range}] y valida
que TargetContent calce en el buffer (el IDE exige match exacto).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://daily-cloudcode-pa.googleapis.com"
OAUTH = os.path.expanduser("~/.gemini/oauth_creds.json")


def oauth_token():
    c = json.load(open(OAUTH))
    exp_ms = c.get("expiry_date") or 0
    # el archivo ya trae un access_token vigente (el refresh NO tiene client_id aquí);
    # usarlo si no venció, igual que hace el sidecar vía TOKFILE/refresh.
    import time

    if c.get("access_token") and exp_ms / 1000 > time.time():
        return c["access_token"]
    tok_file = os.path.expanduser("~/.config/nvim/antigravity_token")
    if os.path.isfile(tok_file):
        t = open(tok_file).read().strip().replace("Bearer ", "")
        if t.startswith("ya29"):
            return t
    try:
        from mitm_harvest import harvest

        live = harvest()
        if live:
            print("token: cosechado del runtime de agy (válido ~1h)")
            return live
    except Exception as e:
        print("mitm-harvest:", e)
    c = json.load(open(OAUTH))
    body = urllib.parse.urlencode({
        "refresh_token": urllib.parse.unquote(c.get("refresh_token") or ""),
        "client_id": c.get("client_id") or "",
        "client_secret": c.get("client_secret") or "",
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["access_token"]


def tools_decl():
    """Declaraciones REALES capturadas del agente (fixture _26, redactado el texto)."""
    return [
        {
            "functionDeclarations": [
                {
                    "name": "replace_file_content",
                    "description": (
                        "Use this tool to edit an existing file. Use ONLY for a SINGLE "
                        "CONTIGUOUS block of edits (replacing one contiguous block of "
                        "text). Specify StartLine, EndLine (1-indexed, StartLine <= "
                        "EndLine), TargetContent (MUST exactly match the file content "
                        "in that range) and ReplacementContent (complete drop-in "
                        "replacement)."
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
                            "TargetFile", "Instruction", "Description",
                            "AllowMultiple", "TargetContent",
                            "ReplacementContent", "StartLine", "EndLine",
                            "toolSummary", "toolAction",
                        ],
                    },
                },
                {
                    "name": "multi_replace_file_content",
                    "description": (
                        "Use this tool ONLY when making MULTIPLE, NON-CONTIGUOUS "
                        "edits to the same file. Each edit is a ReplacementChunk "
                        "with StartLine, EndLine, TargetContent, ReplacementContent."
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
                        "required": [
                            "TargetFile", "Instruction", "Description",
                            "ReplacementChunks", "toolSummary", "toolAction",
                        ],
                    },
                },
            ]
        }
    ]


def parse_edits(buffer_lines, call_name, args):
    """functionCall -> edits neutros [{text, range}]. Valida TargetContent exacto."""
    edits = []

    def one(b, s1, e1, tgt, rep):
        if not (1 <= s1 <= e1 <= len(b)):
            return None
        zone = b[s1 - 1 : e1]
        if "\n".join(zone) != tgt:
            return None
        return {"text": rep, "range": {"start": s1, "endInclusive": e1}}

    try:
        if call_name == "replace_file_content":
            e = one(
                buffer_lines,
                int(args["StartLine"]),
                int(args["EndLine"]),
                args["TargetContent"],
                args["ReplacementContent"],
            )
            if e:
                edits.append(e)
        elif call_name == "multi_replace_file_content":
            for ch in args.get("ReplacementChunks") or []:
                e = one(
                    buffer_lines,
                    int(ch["StartLine"]),
                    int(ch["EndLine"]),
                    ch["TargetContent"],
                    ch["ReplacementContent"],
                )
                if e:
                    edits.append(e)
    except (KeyError, TypeError, ValueError):
        pass
    return edits


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    demo = open(path).read().split("\n") if path else (
        "export interface User {\n  id: number\n"
    ).split("\n")
    row0 = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    col0 = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    cur = demo[row0] if row0 < len(demo) else ""
    marked = [l for l in demo]
    marked[row0] = cur[:col0] + "<|cursor|>" + cur[col0:]
    file_text = "\n".join(marked)
    contents = [
        {
            "role": "user",
            "parts": [
                {
                    "text": (
                        "The cursor is at <|cursor|> in this file. Reply ONLY with a "
                        "replace_file_content (one block) or multi_replace_file_content "
                        "(several blocks) tool call that makes the next logical edit "
                        "at the cursor. Never emit plain text file rewrites.\n\n"
                        + file_text
                    )
                }
            ],
        }
    ]
    body = {
        "project": "aicode-consumers",
        "requestId": "neocursor/spike/" + os.urandom(8).hex(),
        "request": {
            "contents": contents,
            "systemInstruction": {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "You are a tab-completion engine inside an editor. Your ONLY "
                            "allowed output is a replace_file_content or "
                            "multi_replace_file_content tool call editing EXACTLY the "
                            "next logical edit at the <|cursor|> position. "
                            "TargetContent must match the file verbatim. Never return "
                            "plain text, markdown, or explanations."
                        )
                    }
                ],
            },
            "tools": tools_decl(),
            "generationConfig": {"maxOutputTokens": 600},
        },
        "model": "tab_flash_lite_preview",
        "userAgent": "antigravity",
        "requestType": "tab",
    }
    tok = oauth_token()
    h = {
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "antigravity/2.1.1 linux/amd64 google-api-nodejs-client/10.3.0",
    }
    req = urllib.request.Request(
        BASE + "/v1internal:streamGenerateContent?alt=sse",
        data=json.dumps(body).encode(),
        headers=h,
        method="POST",
    )
    try:
        raw = urllib.request.urlopen(req, timeout=90).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode("utf-8", "replace")[:400])
        return 1
    calls = []
    texts = []
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
    print("response: %d functionCall(s), text_len=%d" % (len(calls), sum(map(len, texts))))
    for fc in calls:
        name, args = fc.get("name"), fc.get("args") or {}
        print("--- CALL", name)
        print(json.dumps(args, ensure_ascii=False)[:700])
        edits = parse_edits(demo, name, args)
        print("edits válidos:", len(edits))
        for e in edits:
            print("  L%d-%d text=%r" % (e["range"]["start"], e["range"]["endInclusive"], e["text"][:120]))
    if not calls and texts:
        print("solo texto:", "".join(texts)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())