#!/usr/bin/env python3
"""
neocursor.nvim sidecar — persistent stdio server speaking Cursor's StreamCpp.

Protocol (one JSON object per line, both directions):
  in : {"id":N,"path":str,"content":str,"line":int0,"col":int0,"language":str,
        "additional_files"?:[{path,is_open,last_viewed_at?,ranges:[{start,stop,content}]}],
        "linter_errors"?:{path,errors:[{message,source?,severity,range:{sl,sc,el,ec}}]},
        "file_diff_histories"?:[{file_name,diff_history:[str],diff_history_timestamps:[ms]}]}
  out: {"id":N,"text":str,"range":{...}|null,
        "edits":[{"text":str,"range":{"start":int1,"endInclusive":int1}|null}],
        "prediction":{"path":str,"line":int1}|null}   # text/range == edits[0]
       {"id":N,"error":str}

line/col in the request are 0-indexed (neovim). range in the reply is
1-indexed inclusive (as Cursor's backend returns it).

Launch:  uv run --with 'httpx[http2]' sidecar.py
"""
import os, sys, json, base64, time, sqlite3, struct, socket, subprocess
import httpx

# --- resolver resilience -----------------------------------------------------
# macOS mDNSResponder occasionally negative-caches a CNAME hop (Cursor's hosts
# chain api2 -> api2geo -> api2direct), so getaddrinfo fails while `dig` still
# resolves. Fall back to dig and connect by IP; httpx keeps the hostname for
# SNI/cert verification, so TLS stays valid.
_ORIG_GAI = socket.getaddrinfo


def _dig_ipv4(host: str):
    for cmd in (["dig", "+short", host, "A"], ["host", "-t", "A", host]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except Exception:
            continue
        for ln in out.splitlines():
            tok = ln.strip().rstrip(".").split()[-1:] or [""]
            ip = tok[0]
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                return ip
    return None


def _getaddrinfo(host, port, *a, **kw):
    try:
        return _ORIG_GAI(host, port, *a, **kw)
    except socket.gaierror:
        ip = _dig_ipv4(host)
        if not ip:
            raise
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port or 0)))]


socket.getaddrinfo = _getaddrinfo
# -----------------------------------------------------------------------------

SUP = os.path.expanduser("~/Library/Application Support/Cursor")
GS  = f"{SUP}/User/globalStorage"
URL = "https://api2.cursor.sh/aiserver.v1.AiService/StreamCpp"


def read_token() -> str:
    con = sqlite3.connect(f"file:{GS}/state.vscdb?mode=ro", uri=True)
    row = con.execute(
        "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'"
    ).fetchone()
    con.close()
    if not row:
        raise RuntimeError("no cursorAuth/accessToken — is Cursor signed in?")
    return row[0]


def alf(b: bytes) -> bytes:
    o, t = bytearray(b), 0xA5
    for i in range(len(o)):
        o[i] = ((o[i] ^ t) + (i % 256)) & 0xFF
        t = o[i]
    return bytes(o)


def checksum(mid: str, mac: str) -> str:
    ts = int(time.time() * 1000) // 1_000_000
    return base64.b64encode(alf(ts.to_bytes(6, "big"))).decode() + f"{mid}/{mac}"


def frame(b: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(b)) + b


def deframe(buf: bytes):
    i = 0
    while i + 5 <= len(buf):
        flag = buf[i]
        ln = struct.unpack(">I", buf[i + 1 : i + 5])[0]
        i += 5
        yield flag, buf[i : i + ln]
        i += ln


TOK = read_token()
_sj = json.load(open(f"{GS}/storage.json"))
MID, MAC = _sj["telemetry.machineId"], _sj["telemetry.macMachineId"]
CLIENT = httpx.Client(http2=True, timeout=30)
HEADERS = {
    "x-cursor-client-version": "1.1.3",
    "x-cursor-client-type": "ide",
    "connect-protocol-version": "1",
    "content-type": "application/connect+json",
}
STREAM_URL = URL


def fetch_config() -> dict:
    url = URL.rsplit("/", 1)[0] + "/CppConfig"
    h = dict(HEADERS)
    h["authorization"] = f"Bearer {TOK}"
    h["x-cursor-checksum"] = checksum(MID, MAC)
    h["content-type"] = "application/json"  # CppConfig is unary, not connect+json
    try:
        r = CLIENT.post(url, content=json.dumps({"model": "", "supportsCpt": True}).encode(), headers=h, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _addl(a: dict) -> dict:
    # reshape a neutral nvim range-bundle into Cursor's AdditionalFile message
    ranges = a.get("ranges") or []
    out = {
        "relativeWorkspacePath": a.get("path") or "untitled",
        "isOpen": bool(a.get("is_open")),
        "visibleRangeContent": [r.get("content", "") for r in ranges],
        "startLineNumberOneIndexed": [r.get("start", 1) for r in ranges],
        "visibleRanges": [
            {
                "startLineNumber": r.get("start", 1),
                "endLineNumberInclusive": r.get("stop", r.get("start", 1)),
            }
            for r in ranges
        ],
    }
    if a.get("last_viewed_at") is not None:
        out["lastViewedAt"] = a["last_viewed_at"]
    return out


def _fdh(h: dict) -> dict:
    # neutral nvim bundle -> Cursor CppFileDiffHistory
    out = {
        "fileName": h.get("file_name") or "untitled",
        "diffHistory": h.get("diff_history") or [],
    }
    ts = h.get("diff_history_timestamps")
    if ts:
        out["diffHistoryTimestamps"] = ts
    return out


def _linter(l: dict) -> dict:
    # neutral nvim bundle -> Cursor LinterErrors (positions are 0-indexed)
    errors = []
    for e in l.get("errors") or []:
        item = {"message": e.get("message", ""), "severity": e.get("severity", 1)}
        if e.get("source"):
            item["source"] = e["source"]
        r = e.get("range")
        if r:
            item["range"] = {
                "startPosition": {"line": r.get("sl", 0), "column": r.get("sc", 0)},
                "endPosition": {"line": r.get("el", 0), "column": r.get("ec", 0)},
            }
        errors.append(item)
    return {"relativeWorkspacePath": l.get("path") or "untitled", "errors": errors}


def complete(req: dict) -> dict:
    body = {
        "currentFile": {
            "relativeWorkspacePath": req.get("path") or "untitled",
            "contents": req["content"],
            "cursorPosition": {"line": req["line"], "column": req["col"]},
            "languageId": req.get("language") or "plaintext",
        },
        "modelName": "",
        "diffHistory": [],
        "supportsCpt": True,       # opt into Cursor Prediction Target (tab-tab chain)
        "supportsCrlfCpt": True,
    }
    adds = req.get("additional_files") or []
    if adds:
        body["additionalFiles"] = [_addl(a) for a in adds]
    fdh = req.get("file_diff_histories") or []
    if fdh:
        body["fileDiffHistories"] = [_fdh(h) for h in fdh]
    lint = req.get("linter_errors")
    if lint and lint.get("errors"):
        body["linterErrors"] = _linter(lint)
    h = dict(HEADERS)
    h["authorization"] = f"Bearer {TOK}"
    h["x-cursor-checksum"] = checksum(MID, MAC)
    r = CLIENT.post(STREAM_URL, content=frame(json.dumps(body).encode()), headers=h)

    # The multidiff model streams a SEQUENCE of edits in one response, each
    # bracketed by beginEdit/doneEdit, plus a cursorPredictionTarget pointing at
    # the chain's first jump. Split them into an ordered list so the client can
    # walk the chain locally (one Tab per edit) with no extra round-trips.
    edits, cur, prediction = [], None, None

    def flush():
        nonlocal cur
        if cur is not None and (cur["text"] or cur["range"] is not None):
            t = cur["text"]
            if cur["eol"]:  # shouldRemoveLeadingEol: drop the leading newline
                if t.startswith("\r\n"):
                    t = t[2:]
                elif t.startswith("\n"):
                    t = t[1:]
            edits.append({"text": t, "range": cur["range"]})
        cur = None

    for flag, msg in deframe(r.content):
        if flag & 0x02:
            continue
        try:
            j = json.loads(msg)
        except Exception:
            continue
        if j.get("beginEdit"):
            flush()
            cur = {"text": "", "range": None, "eol": False}
            continue
        if "text" in j:
            if cur is None:
                cur = {"text": "", "range": None, "eol": False}
            cur["text"] += j.get("text", "")
            continue
        rr = j.get("rangeToReplace")
        if rr:
            if cur is None:
                cur = {"text": "", "range": None, "eol": False}
            cur["range"] = {
                "start": rr.get("startLineNumber"),
                "endInclusive": rr.get("endLineNumberInclusive"),
            }
            if j.get("shouldRemoveLeadingEol"):
                cur["eol"] = True
            continue
        cpt = j.get("cursorPredictionTarget")
        if cpt and prediction is None:
            prediction = {
                "path": cpt.get("relativePath"),
                "line": cpt.get("lineNumberOneIndexed"),
            }
        if j.get("doneEdit"):
            flush()
    flush()  # in case the stream ends mid-edit

    first = edits[0] if edits else {"text": "", "range": None}
    return {
        "text": first["text"],
        "range": first["range"],
        "edits": edits,
        "prediction": prediction,
    }


def main():
    global STREAM_URL
    cfg = fetch_config()
    if cfg.get("cppUrl"):
        STREAM_URL = cfg["cppUrl"].rstrip("/") + "/aiserver.v1.AiService/StreamCpp"
    sys.stderr.write("neocursor sidecar ready\n")
    sys.stderr.flush()
    sys.stdout.write(json.dumps({"config": {
        "debounce": cfg.get("clientDebounceDurationMillis"),
        "exclude_patterns": cfg.get("excludeRecentlyViewedFilesPatterns") or [],
        "heuristics": cfg.get("heuristics") or [],
        "reject_hard": (cfg.get("recentlyRejectedEditThresholds") or {}).get("hardRejectThreshold"),
    }}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            res = complete(req)
            res["id"] = rid
        except Exception as e:
            res = {"id": rid, "error": str(e)}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
