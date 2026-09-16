#!/bin/sh
# capture_anty_token.sh — captura el Bearer token real del CLI agy (MITM local
# one-shot) y lo guarda en ~/.config/nvim/antigravity_token (modo 600).
# PERMANENTE POR USUARIO: vale hasta que expire (≈1h de OAuth); recapturá con
# este mismo script cuando <leader>C (RPC) avise "token vencido".
set -e
D=/tmp/anty-mitm-$$
mkdir -p "$D"
TOKFILE="$D/auth.txt"
LEAF="$D/leaf.pem"
TARGET=~/.config/nvim/antigravity_token
HOST=daily-cloudcode-pa.googleapis.com

cleanup() { rm -rf "$D"; }
trap cleanup EXIT

echo "[1/3] certs (openssl)"
nix shell nixpkgs#openssl -c sh -c "
  openssl req -x509 -newkey rsa:2048 -nodes -keyout $D/ca.key -out $D/ca.pem -days 1 -subj /CN=m 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout $D/leaf.key -out $D/leaf.csr -subj /CN=$HOST 2>/dev/null
  printf 'subjectAltName=DNS:$HOST\n' > $D/ext.cnf
  openssl x509 -req -in $D/leaf.csr -CA $D/ca.pem -CAkey $D/ca.key -CAcreateserial -out $LEAF -days 1 -extfile $D/ext.cnf 2>/dev/null
"

echo "[2/3] proxy+agy"
python3 - "$D" "$LEAF" "$TOKFILE" <<'PY' &
import socket, ssl, threading, sys
D, LEAF, TOK = sys.argv[1], sys.argv[2], sys.argv[3]
HOST = "daily-cloudcode-pa.googleapis.com"
ctxs = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctxs.load_cert_chain(LEAF, D+"/leaf.key"); ctxs.set_alpn_protocols(["http/1.1"])
def loglocal(s):
    with open(D+"/reqs.txt","a") as f: f.write(s+"\n")
def rh(c):
    b=b""
    while b"\r\n\r\n" not in b:
        d=c.recv(4096)
        if not d: break
        b+=d
    h,_,r=b.partition(b"\r\n\r\n"); cl=0
    for l in h.split(b"\r\n"):
        if l.lower().startswith(b"content-length:"): cl=int(l.split(b":")[1])
    while len(r)<cl:
        d=c.recv(4096)
        if not d: break
        r+=d
    return h,r[:cl]
def pipe(a,b):
    try:
        while True:
            d=a.recv(32768)
            if not d: break
            b.sendall(d)
    except Exception: pass
    for s in (a,b):
        try: s.shutdown(socket.SHUT_WR)
        except Exception: pass
def tunnel(c,t):
    try:
        h,p=t.rsplit(":",1); u=socket.create_connection((h,int(p)),timeout=10)
        threading.Thread(target=pipe,args=(c,u),daemon=True).start(); pipe(u,c)
    except Exception: c.close()
def handle(c):
    try:
        h=b""
        while b"\r\n\r\n" not in h: h+=c.recv(4096)
        l=h.split(b"\r\n")[0].decode("latin1")
        if not l.startswith("CONNECT"): c.close(); return
        t=l.split()[1]; c.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        if not t.startswith(HOST): tunnel(c,t); return
        tls=ctxs.wrap_socket(c,server_side=True)
        qh,_=rh(tls)
        for ln in qh.decode("latin1").split("\r\n"):
            if "authorization" in ln.lower():
                open(TOK,"w").write(ln.split(":",1)[1].strip()); loglocal("TOK_OK")
        loglocal("REQ "+qh.split(b"\r\n")[0].decode("latin1","replace"))
        tls.close()
    except Exception as e:
        loglocal("ERR "+str(e))
    finally:
        try: c.close()
        except Exception: pass
open(D+"/reqs.txt","w").close()
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("127.0.0.1", 18999)); s.listen(20)
while True:
    c,_=s.accept(); threading.Thread(target=handle,args=(c,),daemon=True).start()
PY
MP=$!
sleep 2
env HTTPS_PROXY=http://127.0.0.1:18999 SSL_CERT_FILE="$D/ca.pem" timeout 25 agy -p "di solo: ok" >/dev/null 2>&1 || true
sleep 1
kill -9 $MP 2>/dev/null || true

echo "[3/3] guardar token"
if [ -s "$TOKFILE" ] && head -c4 "$TOKFILE" | grep -q "^Bea"; then
  mkdir -p "$(dirname "$TARGET")"
  cp "$TOKFILE" "$TARGET"
  chmod 600 "$TARGET"
  echo "✅ token guardado en $TARGET ($(wc -c < "$TARGET") bytes)"
else
  echo "❌ no se capturó el Bearer (¿agy sin sesión?)"
  exit 1
fi