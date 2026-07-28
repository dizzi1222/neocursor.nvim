#!/usr/bin/env python3
r"""Locate Cursor's per-user data directory on macOS, Linux and Windows.

Cursor is an Electron app, so its user-data root is Electron's `userData` —
`appData` plus the app name — i.e. the same layout VS Code uses with "Code"
swapped for "Cursor":

    macOS     ~/Library/Application Support/Cursor
    Linux     $XDG_CONFIG_HOME/Cursor          (default ~/.config/Cursor)
    Windows   %APPDATA%\Cursor                 (…\AppData\Roaming\Cursor)

Below that root the two files the sidecar needs are always at the same relative
paths, on every platform:

    User/globalStorage/state.vscdb    auth token (SQLite)
    User/globalStorage/storage.json   telemetry machine ids

Candidates are probed in order and the first one that actually holds a
state.vscdb wins, which covers Insiders builds and the lowercase directory some
third-party Linux packages create. Two env vars override everything, for
portable installs, WSL and sandboxed packages:

    CURSOR_CONFIG_DIR       the Cursor root shown above
    CURSOR_STATE_DB_PATH    full path to a state.vscdb

Run this file directly for a diagnostic dump (handy in bug reports):

    uv run cursor_paths.py
"""
import os
import sys
from pathlib import Path

_REL_DB = ("User", "globalStorage", "state.vscdb")
_REL_JSON = ("User", "globalStorage", "storage.json")

# Stable, Insiders, and the all-lowercase variant seen in some community Linux
# packages. Order matters: first hit with a state.vscdb wins.
_APP_DIRS = ("Cursor", "Cursor - Insiders", "cursor")


def _home() -> Path:
    # expanduser resolves HOME on POSIX and USERPROFILE / HOMEDRIVE+HOMEPATH on
    # Windows, so this stays correct under both without branching.
    return Path(os.path.expanduser("~"))


def _appdata_roots() -> list:
    """Platform `appData` dirs — the parents of the "Cursor" directory."""
    # Dispatch on sys.platform, not os.name: under Cygwin/MSYS os.name lies, and
    # pathlib keys its concrete class off os.name, so tests can't fake it.
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata)] if appdata else [_home() / "AppData" / "Roaming"]
    if sys.platform == "darwin":
        return [_home() / "Library" / "Application Support"]
    # Linux and the BSDs: XDG first, then the config roots that Flatpak and
    # Snap sandboxes relocate to (neither is an official Cursor channel, but
    # both are cheap to probe and users do build them).
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return [
        Path(xdg) if xdg else _home() / ".config",
        _home() / ".var" / "app" / "co.anysphere.cursor" / "config",
        _home() / "snap" / "cursor" / "current" / ".config",
    ]


def candidates() -> list:
    """Every directory we'd accept as Cursor's user-data root, best first."""
    env = os.environ.get("CURSOR_CONFIG_DIR")
    if env:
        return [Path(env).expanduser()]
    db = os.environ.get("CURSOR_STATE_DB_PATH")
    if db:
        p = Path(db).expanduser()
        # …/<root>/User/globalStorage/state.vscdb -> <root>
        return [p.parents[2]] if len(p.parents) >= 3 else [p.parent]
    return [root / app for root in _appdata_roots() for app in _APP_DIRS]


def support_dir() -> Path:
    """Cursor's user-data root. Falls back to the canonical path for this OS."""
    cands = candidates()
    for c in cands:
        if c.joinpath(*_REL_DB).is_file():
            return c
    return cands[0]


def state_db() -> Path:
    """Path to state.vscdb (the SQLite store holding cursorAuth/accessToken)."""
    db = os.environ.get("CURSOR_STATE_DB_PATH")
    if db:
        return Path(db).expanduser()
    return support_dir().joinpath(*_REL_DB)


def state_db_uri(read_only: bool = True) -> str:
    """A SQLite URI for state_db().

    as_uri() percent-encodes the space in "Application Support" and emits the
    file:///C:/… form Windows needs; SQLite decodes both. Building the URI by
    hand breaks on one platform or the other.
    """
    uri = state_db().absolute().as_uri()
    return uri + "?mode=ro" if read_only else uri


def storage_json() -> Path:
    """Path to storage.json (telemetry.machineId / telemetry.macMachineId)."""
    return support_dir().joinpath(*_REL_JSON)


def searched() -> list:
    """Human-readable candidate list, for "not found" errors."""
    return [str(c) for c in candidates()]


def not_found_message() -> str:
    lines = ["Cursor's state.vscdb not found. Looked for:"]
    lines += [f"  {c}{os.sep}" + os.sep.join(_REL_DB) for c in candidates()]
    lines.append(
        "Is Cursor installed and signed in? If it lives somewhere unusual, set "
        "CURSOR_CONFIG_DIR (the Cursor data dir) or CURSOR_STATE_DB_PATH."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    db = state_db()
    print(f"platform      {sys.platform} (os.name={os.name})")
    print(f"support_dir   {support_dir()}")
    print(f"state.vscdb   {db}  {'OK' if db.is_file() else 'MISSING'}")
    sj = storage_json()
    print(f"storage.json  {sj}  {'OK' if sj.is_file() else 'MISSING'}")
    print(f"sqlite uri    {state_db_uri()}")
    print("candidates:")
    for c in searched():
        print(f"  {c}")
    sys.exit(0 if db.is_file() else 1)
