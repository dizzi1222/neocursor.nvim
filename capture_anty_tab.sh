#!/bin/sh
# capture_anty_tab.sh — captura request+BODY y respuesta de streamGenerateContent
# (familia tab/supercomplete) vía MITM local. Token redactado; fixtures en
# tests/fixtures/.
#   ./capture_anty_tab.sh          # CLI: hace un agy -p de ejemplo
#   ./capture_anty_tab.sh cli-edit # CLI: tarea de edición (dispara tools/edit)
#   ./capture_anty_tab.sh ide      # IDE: proxy vivo hasta que presiones Enter
#   ./capture_anty_tab.sh --ide    # igual que "ide"
set -e
ROOT=$(cd "$(dirname "$0")" && pwd)
D=/tmp/anty-mitm-$$
mkdir -p "$D"
mkdir -p "$ROOT/tests/fixtures"
LEAF="$D/leaf.pem"
HOST=daily-cloudcode-pa.googleapis.com
TARGET_DIR="$ROOT/tests/fixtures"
MODE=$(printf '%s' "${1:-cli}" | sed 's/^--//')   # normaliza: --ide -> ide

cleanup() { rm -rf "$D"; }
trap cleanup EXIT

echo "[1/3] certs (openssl via nix)"
nix shell nixpkgs#openssl -c sh -c "
  openssl req -x509 -newkey rsa:2048 -nodes -keyout $D/ca.key -out $D/ca.pem -days 1 -subj /CN=m 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout $D/leaf.key -out $D/leaf.csr -subj /CN=$HOST 2>/dev/null
  printf 'subjectAltName=DNS:$HOST\n' > $D/ext.cnf
  openssl x509 -req -in $D/leaf.csr -CA $D/ca.pem -CAkey $D/ca.key -CAcreateserial -out $LEAF -days 1 -extfile $D/ext.cnf 2>/dev/null
"

echo "[2/3] MITM proxy (18999) → tests/fixtures/"
python3 - "$D" "$LEAF" "$TARGET_DIR" <<'PY' &
import socket, ssl, threading, sys, time, os, json, re
D, LEAF, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
HOST = "daily-cloudcode-pa.googleapis.com"
ctxs = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctxs.load_cert_chain(LEAF, D+"/leaf.key")
ctxs.set_alpn_protocols(["http/1.1"])
nreq = {"n": 0}
LOCK = threading.Lock()
MAXREQ = int(os.environ.get("ANTY_CAPTURE_MAX", "40"))
CONND = os.path.join(OUT, "connects_%d.log" % int(time.time()))

def log_conn(target, tag):
    try:
        with open(CONND, "a") as f:
            f.write("%s %s\n" % (tag, target))
    except Exception:
        pass

def read_head_and_body(c):
    b = b""
    while b"\r\n\r\n" not in b:
        d = c.recv(8192)
        if not d: return None, None
        b += d
    h, _, tail = b.partition(b"\r\n\r\n")
    cl = 0; chunked = False
    for l in h.split(b"\r\n"):
        low = l.lower()
        if low.startswith(b"content-length:"): cl = int(l.split(b":")[1])
        if low.startswith(b"transfer-encoding:") and b"chunked" in low: chunked = True
    body = tail
    while len(body) < cl and cl:
        d = c.recv(65536)
        if not d: break
        body += d
    if chunked and not body.endswith(b"0\r\n\r\n"):
        while not body.endswith(b"0\r\n\r\n"):
            d = c.recv(65536)
            if not d: break
            body += d
    return h, body

def relay(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except Exception:
        pass
    try: b.shutdown(socket.SHUT_WR)
    except Exception: pass

def tunnel(c, target):
    try:
        hp, pt = target.rsplit(":", 1)
        u = socket.create_connection((hp, int(pt)), timeout=12)
        threading.Thread(target=relay, args=(c, u), daemon=True).start()
        relay(u, c)
    except Exception:
        try: c.close()
        except Exception: pass

def handle(c):
    try:
        h = b""
        while b"\r\n\r\n" not in h:
            d = c.recv(8192)
            if not d: break
            h += d
        if not h: c.close(); return
        line = h.split(b"\r\n")[0].decode("latin1", "replace")
        if not line.startswith("CONNECT"):
            c.close(); return
        target = line.split()[1]
        c.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        if not target.startswith(HOST):
            log_conn(target, "blind")
            tunnel(c, target)
            return
        tls = ctxs.wrap_socket(c, server_side=True)
        qh, qbody = read_head_and_body(tls)
        if qh is None:
            tls.close(); return
        qline = qh.split(b"\r\n")[0].decode("latin1", "replace")
        mline, path = qline.split(" ", 2)[:2]
        up = ssl.create_default_context().wrap_socket(
            socket.create_connection((HOST, 443), timeout=15), server_hostname=HOST)
        up.sendall(qh + b"\r\nConnection: close\r\n\r\n" + qbody)
        found = b""
        try:
            while b"\r\n\r\n" not in found:
                d = up.recv(65536)
                if not d: break
                found += d
            rhh, _, rtail = found.partition(b"\r\n\r\n")
            cl = 0
            for l in rhh.split(b"\r\n"):
                if l.lower().startswith(b"content-length:"): cl = int(l.split(b":")[1])
            rbody = rtail
            while cl and len(rbody) < cl:
                d = up.recv(65536)
                if not d: break
                rbody += d
            while True:
                d = up.recv(65536)
                if not d: break
                rbody += d
        except Exception:
            pass
        try: tls.sendall(rhh + b"\r\n\r\n" + rbody)
        except Exception: pass
        with LOCK:
            nreq["n"] += 1
            k = nreq["n"]
        if k <= MAXREQ:
            qh2 = re.sub(rb"(?i)authorization:\s*Bearer\s+\S+", b"authorization: Bearer <REDACTED>", qh)
            obj = {"method": mline.strip(), "path": path.strip(),
                   "request_headers": qh2.decode("latin1", "replace"),
                   "request_body": qbody.decode("utf-8", "replace"),
                   "response_headers": rhh.decode("latin1", "replace"),
                   "response_body": rbody.decode("utf-8", "replace")}
            fname = "tab_%d_%d.json" % (int(time.time()), k)
            with open(os.path.join(OUT, fname), "w") as f:
                json.dump(obj, f)
            print("→ %s" % fname)
        try: up.close()
        except Exception: pass
        try: tls.close()
        except Exception: pass
    except Exception as e:
        try:
            with open(D+"/err.log", "a") as f: f.write(str(e) + "\n")
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass

s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 18999)); s.listen(64)
print("MITM escuchando en 18999")
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
PY
MP=$!
sleep 2

if [ "$MODE" = "ide" ]; then
  echo
  echo "[3/3] MODO IDE — proxy VIVO hasta que presiones Enter"
  echo "  1) Abrí el IDE con:"
  echo "     env HTTPS_PROXY=http://127.0.0.1:18999 antigravity-ide \\"
  echo "         --proxy-server=http://127.0.0.1:18999 --ignore-certificate-errors"
  echo "  2) Logueate (el login también pasa por el túnel ciego)."
  echo "  3) Abrí /home/diego/workspace/tmp-testing/supercomplete/data.ts"
  echo "  4) Puntito en cursor y hacé 2-3 Tabs (aceptar ghost)."
  echo
  read -r _
else
  if [ "$MODE" = "cli-edit" ]; then
    echo "[3/3] CLI-EDIT: mini-archivo en /tmp + tarea de edición por el proxy"
    rm -rf /tmp/opencode/antytest && mkdir -p /tmp/opencode/antytest
    printf 'let total = 0;\nfor (let i = 0; i < 5; i++) {\n  console.log(i);\n}\n' > /tmp/opencode/antytest/test.txt
    env HTTPS_PROXY=http://127.0.0.1:18999 SSL_CERT_FILE="$D/ca.pem" \
      timeout 90 agy -p --add-dir /tmp/opencode/antytest \
      "En test.txt, dentro del for, suma i a total y muestra ambos en el console.log. Solo hacé ese cambio." >/dev/null 2>&1 || true
  else
    echo "[3/3] CLI: agy -p por el proxy (1 request de ejemplo)"
    env HTTPS_PROXY=http://127.0.0.1:18999 SSL_CERT_FILE="$D/ca.pem" timeout 45 agy -p "di solo: ok" >/dev/null 2>&1 || true
  fi
fi
sleep 1
kill -9 $MP 2>/dev/null || true

echo "Fixtures en tests/fixtures/:"
ls -1 "$ROOT/tests/fixtures/" | tail -12