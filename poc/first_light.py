#!/usr/bin/env python3
"""tabtab.nvim first light: real Tab completion from Cursor's StreamCpp."""
import os, json, base64, time, sqlite3, struct
import httpx

SUP = os.path.expanduser("~/Library/Application Support/Cursor")
def read_token():
    con = sqlite3.connect(f"file:{SUP}/User/globalStorage/state.vscdb?mode=ro", uri=True)
    v = con.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()[0]
    con.close(); return v
def alf(b):
    o,t=bytearray(b),0xA5
    for i in range(len(o)): o[i]=((o[i]^t)+(i%256))&0xFF; t=o[i]
    return bytes(o)
def checksum(mid,mac):
    ts=int(time.time()*1000)//1_000_000
    return base64.b64encode(alf(ts.to_bytes(6,"big"))).decode()+f"{mid}/{mac}"
def env(b): return b"\x00"+struct.pack(">I",len(b))+b
def deframe(buf):
    i=0
    while i+5<=len(buf):
        flag=buf[i]; ln=struct.unpack(">I",buf[i+1:i+5])[0]; i+=5
        yield flag, buf[i:i+ln]; i+=ln

tok=read_token()
sj=json.load(open(f"{SUP}/User/globalStorage/storage.json"))
mid,mac=sj["telemetry.machineId"],sj["telemetry.macMachineId"]
H={"authorization":f"Bearer {tok}","x-cursor-checksum":checksum(mid,mac),
   "x-cursor-client-version":"1.1.3","x-cursor-client-type":"ide",
   "connect-protocol-version":"1","content-type":"application/connect+json"}

req={
  "currentFile":{
    "relativeWorkspacePath":"demo.py",
    "contents":"def fibonacci(n):\n    # return the nth fibonacci number\n    ",
    "cursorPosition":{"line":2,"column":4},
    "languageId":"python",
  },
  "modelName":"",
  "diffHistory":[],
}
url="https://api2.cursor.sh/aiserver.v1.AiService/StreamCpp"
with httpx.Client(http2=True, timeout=30) as c:
    r=c.post(url, content=env(json.dumps(req).encode()), headers=H)
print("HTTP", r.status_code)
text=""; frames=0
for flag,msg in deframe(r.content):
    frames+=1
    if flag&0x02:
        print("[end frame]", msg.decode()[:150]); continue
    try:
        j=json.loads(msg)
        text+=j.get("text","")
        if "rangeToReplace" in j or "cursorPredictionTarget" in j:
            print("[meta]", {k:j[k] for k in j if k not in ("text",)})
    except Exception as e: print("parse?", msg[:80])
print(f"\nframes={frames}")
print("=== COMPLETION ===")
print(text if text else "(empty)")
