#!/usr/bin/env python3
"""Cosecha el Bearer OAuth FRESCO del runtime de agy (MITM one-shot).

agy renueva sus tokens en su propio proceso; los archivos pueden estar vencidos.
Esto levanta un proxy CONNECT en 127.0.0.1:18998, corre `agy -p` con
HTTPS_PROXY/SSL_CERT_FILE y lee el header Authorization de su propia llamada
a daily-cloudcode-pa.googleapis.com. No persiste nada en disco.
"""
import os
import socket
import ssl
import subprocess
import threading
import time

D = "/tmp/opencode-mitm"
HOST = "daily-cloudcode-pa.googleapis.com"


def harvest(timeout=60):
    # si los certs ya existen (generados antes), reusarlos; si no, nix shell
    need = not (
        os.path.isfile(f"{D}/ca.pem") and os.path.isfile(f"{D}/leaf.pem")
        and os.path.isfile(f"{D}/leaf.key")
    )
    if need:
        import shutil
        import subprocess

        for f in ("ca.pem", "ca.key", "leaf.pem", "leaf.key", "leaf.csr", "ext.cnf", "ca.srl"):
            try:
                os.unlink(os.path.join(D, f))
            except OSError:
                pass
        host = "daily-cloudcode-pa.googleapis.com"
        try:
            subprocess.run(
                ["nix", "shell", "nixpkgs#openssl", "-c", "sh", "-c",
                 f"openssl req -x509 -newkey rsa:2048 -nodes -keyout {D}/ca.key "
                 f"-out {D}/ca.pem -days 1 -subj /CN=spike 2>/dev/null && "
                 f"openssl req -newkey rsa:2048 -nodes -keyout {D}/leaf.key "
                 f"-out {D}/leaf.csr -subj '/CN={host}' 2>/dev/null && "
                 f"printf 'subjectAltName=DNS:{host}\\n' > {D}/ext.cnf && "
                 f"openssl x509 -req -in {D}/leaf.csr -CA {D}/ca.pem "
                 f"-CAkey {D}/ca.key -CAcreateserial -out {D}/leaf.pem "
                 f"-days 1 -extfile {D}/ext.cnf 2>/dev/null"],
                capture_output=True, timeout=120)
        except Exception:
            pass
        if not (os.path.isfile(f"{D}/ca.pem") and os.path.isfile(f"{D}/leaf.pem")):
            print("mitm: sin certs")
            return None
    got = {}
    ctxs = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctxs.load_cert_chain(f"{D}/leaf.pem", f"{D}/leaf.key")
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 18998))
    srv.listen(16)

    def handle(c):
        try:
            h = b""
            while b"\r\n\r\n" not in h:
                d = c.recv(8192)
                if not d:
                    c.close()
                    return
                h += d
            line = h.split(b"\r\n")[0].decode("latin1", "replace")
            if not line.startswith("CONNECT"):
                c.close()
                return
            target = line.split()[1]
            c.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            if target.startswith(HOST):
                try:
                    tls = ctxs.wrap_socket(c, server_side=True)
                except Exception:
                    c.close()
                    return
                b = b""
                try:
                    while b"\r\n\r\n" not in b:
                        d = tls.recv(8192)
                        if not d:
                            break
                        b += d
                except Exception:
                    pass
                for ln in b.decode("latin1", "replace").split("\r\n"):
                    if ln.lower().startswith("authorization:"):
                        got["tok"] = ln.split(":", 1)[1].strip()
                        break
                try:
                    tls.sendall(b"HTTP/1.1 502 x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except Exception:
                    pass
                try:
                    tls.close()
                except Exception:
                    pass
            else:
                try:
                    hp, pt = target.rsplit(":", 1)
                    u = socket.create_connection((hp, int(pt)), timeout=10)
                except Exception:
                    c.close()
                    return

                def fwd(a, b):
                    try:
                        while True:
                            d = a.recv(65536)
                            if not d:
                                break
                            b.sendall(d)
                    except Exception:
                        pass

                threading.Thread(target=fwd, args=(u, c), daemon=True).start()
                fwd(c, u)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass

    def serve():
        end = time.time() + timeout
        srv.settimeout(2)
        while time.time() < end and "tok" not in got:
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(c,), daemon=True).start()
        try:
            srv.close()
        except Exception:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    env = dict(
        os.environ,
        HTTPS_PROXY="http://127.0.0.1:18998",
        HTTP_PROXY="http://127.0.0.1:18998",
        SSL_CERT_FILE=f"{D}/ca.pem",
        NO_PROXY="localhost,127.0.0.1",
    )
    try:
        subprocess.run(["agy", "-p", "di solo: ok"], env=env,
                       capture_output=True, timeout=timeout - 5)
    except Exception:
        pass
    t.join(timeout=8)
    tok = got.get("tok", "")
    if tok.lower().startswith("bearer "):
        return tok.split(None, 1)[1]
    return tok or None


if __name__ == "__main__":
    t = harvest()
    print("OK" if t else "SIN TOKEN")
