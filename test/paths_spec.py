#!/usr/bin/env python3
"""Cross-platform path resolution spec for cursor_paths.py.

Runs anywhere — each case fakes a platform (os.name / sys.platform), plants a
state.vscdb in a temp tree, and asserts the resolver lands on it. So the
Linux/Windows branches are covered from a macOS checkout and vice versa.

    uv run test/paths_spec.py          # or: python3 test/paths_spec.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cursor_paths  # noqa: E402

REL_DB = ("User", "globalStorage", "state.vscdb")
_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got  {got}\n       want {want}")
        _fails.append(name)


def plant(root: Path) -> Path:
    """Create <root>/User/globalStorage/{state.vscdb,storage.json}."""
    gs = root.joinpath(*REL_DB[:-1])
    gs.mkdir(parents=True, exist_ok=True)
    gs.joinpath("state.vscdb").write_bytes(b"")
    gs.joinpath("storage.json").write_text("{}")
    return root


def env(**over):
    """Clean slate for every var the resolver reads, plus the case's overrides."""
    base = dict(os.environ)
    for k in ("CURSOR_CONFIG_DIR", "CURSOR_STATE_DB_PATH", "XDG_CONFIG_HOME", "APPDATA"):
        base.pop(k, None)
    base.update({k: str(v) for k, v in over.items()})
    return mock.patch.dict(os.environ, base, clear=True)


def as_mac():
    return mock.patch.object(sys, "platform", "darwin")


def as_linux():
    return mock.patch.object(sys, "platform", "linux")


def as_windows():
    # only sys.platform is faked — os.name stays "posix" so pathlib keeps
    # building PosixPath, which is what this host can actually instantiate
    return mock.patch.object(sys, "platform", "win32")


def case(title, platform_patch, env_patch, body):
    print(title)
    with platform_patch, env_patch:
        body()


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # --- macOS: ~/Library/Application Support/Cursor ---------------------------
    home = plant(tmp / "mac" / "Library" / "Application Support" / "Cursor").parents[2]
    case(
        "macOS — Library/Application Support",
        as_mac(),
        env(HOME=home),
        lambda: (
            check(
                "support_dir",
                cursor_paths.support_dir(),
                home / "Library" / "Application Support" / "Cursor",
            ),
            check(
                "state_db",
                cursor_paths.state_db(),
                home / "Library" / "Application Support" / "Cursor" / Path(*REL_DB),
            ),
            # the space must be percent-encoded, and mode=ro kept
            check(
                "uri escapes space",
                "Application%20Support" in cursor_paths.state_db_uri(),
                True,
            ),
            check("uri is read-only", cursor_paths.state_db_uri().endswith("?mode=ro"), True),
        ),
    )

    # --- Linux: XDG_CONFIG_HOME wins over ~/.config ---------------------------
    xdg = plant(tmp / "xdg" / "Cursor").parent
    case(
        "Linux — $XDG_CONFIG_HOME/Cursor",
        as_linux(),
        env(HOME=tmp / "nohome", XDG_CONFIG_HOME=xdg),
        lambda: check("support_dir", cursor_paths.support_dir(), xdg / "Cursor"),
    )

    # --- Linux: default ~/.config/Cursor (the reported bug) -------------------
    lhome = plant(tmp / "lin" / ".config" / "Cursor").parents[1]
    case(
        "Linux — default ~/.config/Cursor",
        as_linux(),
        env(HOME=lhome),
        lambda: check("support_dir", cursor_paths.support_dir(), lhome / ".config" / "Cursor"),
    )

    # --- Linux: lowercase dir from third-party packages ----------------------
    # Asserted by "the db resolves", not by dir name: a case-insensitive host FS
    # (macOS) matches the capitalised candidate against a lowercase directory.
    lc = plant(tmp / "lc" / ".config" / "cursor").parents[1]
    case(
        "Linux — lowercase ~/.config/cursor",
        as_linux(),
        env(HOME=lc),
        lambda: check("state_db resolves", cursor_paths.state_db().is_file(), True),
    )

    # --- Windows: %APPDATA%\Cursor -------------------------------------------
    appdata = plant(tmp / "win" / "AppData" / "Roaming" / "Cursor").parent
    case(
        "Windows — %APPDATA%\\Cursor",
        as_windows(),
        env(APPDATA=appdata, HOME=tmp / "win"),
        lambda: check("support_dir", cursor_paths.support_dir(), appdata / "Cursor"),
    )

    # --- Windows: APPDATA unset -> ~/AppData/Roaming --------------------------
    whome = plant(tmp / "win2" / "AppData" / "Roaming" / "Cursor").parents[2]
    case(
        "Windows — APPDATA unset, falls back to ~/AppData/Roaming",
        as_windows(),
        env(HOME=whome),
        lambda: check(
            "support_dir",
            cursor_paths.support_dir(),
            whome / "AppData" / "Roaming" / "Cursor",
        ),
    )

    # --- Insiders channel ----------------------------------------------------
    ins = plant(tmp / "ins" / ".config" / "Cursor - Insiders").parents[1]
    case(
        "Linux — Cursor - Insiders",
        as_linux(),
        env(HOME=ins),
        lambda: check(
            "support_dir", cursor_paths.support_dir(), ins / ".config" / "Cursor - Insiders"
        ),
    )

    # --- Overrides -----------------------------------------------------------
    custom = plant(tmp / "custom" / "CursorPortable")
    case(
        "CURSOR_CONFIG_DIR overrides everything",
        as_linux(),
        env(HOME=lhome, CURSOR_CONFIG_DIR=custom),
        lambda: (
            check("support_dir", cursor_paths.support_dir(), custom),
            check("state_db", cursor_paths.state_db(), custom / Path(*REL_DB)),
        ),
    )

    odd = tmp / "odd" / "somewhere" / "cursor.sqlite"
    odd.parent.mkdir(parents=True)
    odd.write_bytes(b"")
    case(
        "CURSOR_STATE_DB_PATH points straight at the db",
        as_linux(),
        env(HOME=lhome, CURSOR_STATE_DB_PATH=odd),
        lambda: check("state_db", cursor_paths.state_db(), odd),
    )

    # --- Nothing installed: report the canonical path, don't crash -----------
    empty = tmp / "empty"
    empty.mkdir()
    case(
        "no install — canonical path + actionable error text",
        as_linux(),
        env(HOME=empty),
        lambda: (
            check("support_dir", cursor_paths.support_dir(), empty / ".config" / "Cursor"),
            check(
                "message names the override",
                "CURSOR_CONFIG_DIR" in cursor_paths.not_found_message(),
                True,
            ),
        ),
    )

# --- the URI a real Windows path yields, checkable from any host -------------
# PureWindowsPath is constructible everywhere, so the exact string SQLite will be
# handed on Windows can be asserted from macOS/Linux. This is the form that a
# hand-built f"file:{path}" got wrong: backslashes and a bare drive letter.
print("Windows SQLite URI shape (via PureWindowsPath)")
from pathlib import PureWindowsPath  # noqa: E402

win_uri = PureWindowsPath(
    r"C:\Users\me\AppData\Roaming\Cursor\User\globalStorage\state.vscdb"
).as_uri()
check(
    "drive letter + forward slashes",
    win_uri,
    "file:///C:/Users/me/AppData/Roaming/Cursor/User/globalStorage/state.vscdb",
)
check("no backslashes survive", "\\" not in win_uri, True)

# --- live check on the host, if Cursor is actually installed -----------------
real = cursor_paths.state_db()
print(f"live host ({sys.platform}) — {real}")
print(f"  {'ok  ' if real.is_file() else 'skip'} real state.vscdb present")

print(f"\n{'FAILED: ' + ', '.join(_fails) if _fails else 'all path cases passed'}")
sys.exit(1 if _fails else 0)
