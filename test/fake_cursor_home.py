#!/usr/bin/env python3
"""Synthesize a credential-free Cursor install, for cross-platform tests.

    python3 test/fake_cursor_home.py <root>

Writes exactly what sidecar.py reads at startup:

    <root>/User/globalStorage/state.vscdb    ItemTable row w/ a dummy token
    <root>/User/globalStorage/storage.json   dummy telemetry machine ids

That's enough to exercise the whole startup path — path resolution, the SQLite
URI, the stdio handshake — on a Windows or Linux runner where no one is signed
into Cursor. The token is deliberately invalid: the backend rejects it, which is
fine, because CppConfig failures are swallowed by design.
"""
import json
import sqlite3
import sys
from pathlib import Path

TOKEN = "not-a-real-token.fake-for-ci"


def build(root: Path) -> Path:
    gs = root / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    db = gs / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
    con.execute(
        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
        ("cursorAuth/accessToken", TOKEN),
    )
    con.commit()
    con.close()
    gs.joinpath("storage.json").write_text(
        json.dumps({"telemetry.machineId": "0" * 64, "telemetry.macMachineId": "1" * 64}),
        encoding="utf-8",
    )
    return db


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(build(Path(sys.argv[1]).expanduser()))
