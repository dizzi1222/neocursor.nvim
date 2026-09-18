#!/usr/bin/env python3
"""Harvest antigravity Bearer via pty interactivo + MITM (porque `agy -p` quedó degradado).

Levanta proxy CONNECT en 18998 (reusa certs de /tmp/opencode-mitm), abre `agy`
interactivo con HTTPS_PROXY/SSL_CERT_FILE y captura el header Authorization del
request a daily-cloudcode-pa.googleapis.com. Luego mata agy. Sale con el token.
"""
import os
import pty
import socket
import ssl
import subprocess
import threading
import time

D = "/tmp/opencode-mitm"
HOST = "daily-cloudcode-pa.googleapis.com"


def harvest(timeout=60):
    if not (os.path.isfile(f"{D}/ca.pem") and os.path.isfile(f"{D}/leaf.pem")):
        print("mitm: sin certs (corré antes mitm gen o volvé a capturar)")
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
    master, slave = pty.openpty()
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        proc = subprocess.Popen(
            ["agy"], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
        )
        os.close(slave)
        time.sleep(4)
        try:
            os.write(master, b"/usage\r")
        except OSError:
            pass
        # esperar/consumir mientras el TUI hace la llamada de quota
        end = time.time() + min(20, timeout - 5)
        while time.time() < end and "tok" not in got:
            try:
                import select as sel

                r, _, _ = sel.select([master], [], [], 1.0)
                if r:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            except Exception:
                break
        proc.kill()
    except Exception as e:
        print("pty err", e)
    finally:
        try:
            os.close(master)
        except Exception:
            pass

    t.join(timeout=8)
    tok = got.get("tok", "")
    if tok.lower().startswith("bearer "):
        return tok.split(None, 1)[1]
    return tok or None


if __name__ == "__main__":
    t = harvest()
    print(("OK " + t[:10] + "…") if t else "SIN TOKEN")