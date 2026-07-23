#!/usr/bin/env python3
"""Discover the real Cpp/Tab backend by calling CppService/CppConfig (unary)."""
import os, json, base64, time, sqlite3, struct, sys
import httpx

SUP = os.path.expanduser("~/Library/Application Support/Cursor")

def read_token():
    con = sqlite3.connect(f"file:{SUP}/User/globalStorage/state.vscdb?mode=ro", uri=True)
    row = con.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()
    con.close(); return row[0]

def alf(b):
    out, t = bytearray(b), 0xA5
    for i in range(len(out)):
        out[i] = ((out[i] ^ t) + (i % 256)) & 0xFF; t = out[i]
    return bytes(out)

def checksum(mid, mac):
    ts = int(time.time()*1000)//1_000_000
    return base64.b64encode(alf(ts.to_bytes(6,"big"))).decode()+f"{mid}/{mac}"

def env(obj):
    b = json.dumps(obj).encode(); return b"\x00"+struct.pack(">I",len(b))+b

def deframe(buf):
    i=0
    while i+5<=len(buf):
        flag=buf[i]; ln=struct.unpack(">I",buf[i+1:i+5])[0]; i+=5
        yield flag, buf[i:i+ln]; i+=ln

tok = read_token()
sj = json.load(open(f"{SUP}/User/globalStorage/storage.json"))
mid, mac = sj["telemetry.machineId"], sj["telemetry.macMachineId"]
H = {
    "authorization": f"Bearer {tok}",
    "x-cursor-checksum": checksum(mid, mac),
    "x-cursor-client-type": "ide",
    "x-cursor-client-version": "1.1.3",
    "content-type": "application/connect+json",
    "connect-protocol-version": "1",
}

hosts = ["api2.cursor.sh","api3.cursor.sh","api4.cursor.sh","repo42.cursor.sh"]
services = ["aiserver.v1.CppService/CppConfig","aiserver.v1.AiService/CppConfig"]
with httpx.Client(http2=True, timeout=20) as c:
    for h in hosts:
        for s in services:
            url=f"https://{h}/{s}"
            try: r=c.post(url, content=env({}), headers=H)
            except Exception as e: print(f"{h:20} {s:34} ERR {str(e)[:40]}"); continue
            note=""
            if r.status_code==200:
                for flag,msg in deframe(r.content):
                    if flag==0 and msg:
                        try: note=json.dumps(json.loads(msg))[:400]
                        except: note=msg[:120].hex()
            else:
                note=r.text[:150].replace("\n"," ")
            print(f"{h:20} {s.split('/')[-1]:10} HTTP {r.status_code}  {note}")
