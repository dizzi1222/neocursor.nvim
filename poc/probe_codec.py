#!/usr/bin/env python3
"""Probe AiService with correct Connect codecs. Unary=raw body; streaming=enveloped."""
import os, json, base64, time, sqlite3, struct, sys
import httpx

# poc/ sits one level below the repo root; put it on sys.path for cursor_paths.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cursor_paths
def read_token():
    con = sqlite3.connect(cursor_paths.state_db_uri(), uri=True)
    v = con.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()[0]
    con.close(); return v
def alf(b):
    o,t=bytearray(b),0xA5
    for i in range(len(o)): o[i]=((o[i]^t)+(i%256))&0xFF; t=o[i]
    return bytes(o)
def checksum(mid,mac):
    ts=int(time.time()*1000)//1_000_000
    return base64.b64encode(alf(ts.to_bytes(6,"big"))).decode()+f"{mid}/{mac}"

tok=read_token()
sj=json.load(open(cursor_paths.storage_json(), encoding="utf-8"))
mid,mac=sj["telemetry.machineId"],sj["telemetry.macMachineId"]
base={"authorization":f"Bearer {tok}","x-cursor-checksum":checksum(mid,mac),
      "x-cursor-client-version":"1.1.3","x-cursor-client-type":"ide","connect-protocol-version":"1"}

def show(tag,r):
    body=r.text[:180].replace("\n"," ")
    print(f"{tag:58} HTTP {r.status_code}  {body}")

with httpx.Client(http2=True, timeout=20) as c:
    # --- unary CppConfig: raw body (no envelope) ---
    for ct in ("application/json","application/proto"):
        h=dict(base); h["content-type"]=ct
        payload=b"{}" if ct.endswith("json") else b""      # empty proto msg = 0 bytes
        r=c.post("https://api2.cursor.sh/aiserver.v1.AiService/CppConfig", content=payload, headers=h)
        show(f"CppConfig unary [{ct}]", r)
    # --- streaming StreamCpp: enveloped, both codecs ---
    def env(b): return b"\x00"+struct.pack(">I",len(b))+b
    for ct in ("application/connect+json","application/connect+proto"):
        h=dict(base); h["content-type"]=ct
        payload=env(b"{}") if ct.endswith("json") else env(b"")
        r=c.post("https://api2.cursor.sh/aiserver.v1.AiService/StreamCpp", content=payload, headers=h)
        show(f"StreamCpp stream [{ct}]", r)
