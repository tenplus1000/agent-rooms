"""本機路徑：不寫死任何人的帳號。每一項都可以用環境變數蓋掉。"""

import functools
import os
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=1)
def windows_home():
    """WSL 上的 Windows 使用者資料夾（例如 /mnt/c/Users/<名字>）；不是 WSL 就回 None。"""
    given = os.environ.get("ROOMS_WINDOWS_HOME", "").strip()
    if given:
        return Path(given)
    try:
        if "microsoft" not in Path("/proc/version").read_text().lower():
            return None
        win = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True,
                             text=True, timeout=5, cwd="/mnt/c").stdout.strip()
        if not win or "%" in win:
            return None
        path = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True, timeout=5).stdout.strip()
        return Path(path) if path else None
    except Exception:
        return None


def acc_sessions():
    """acc（Agent Context Controller）的 session 紀錄：用來看使用者最後一次在某個對話講話的時間。"""
    given = os.environ.get("ACC_SESSIONS", "").strip()
    if given:
        return Path(given)
    try:
        root = (Path.home() / ".config" / "acc" / "root").read_text(encoding="utf-8").strip()
        if root:
            return Path(root) / "goals" / "sessions"
    except OSError:
        pass
    return (windows_home() or Path.home()) / ".agent-context" / "goals" / "sessions"


def desktop_codex_sessions():
    """Windows 桌面版 Codex 的對話紀錄；不在 WSL 就回 None。"""
    given = os.environ.get("ROOMS_DESKTOP_SESSIONS", "").strip()
    if given:
        return Path(given)
    home = windows_home()
    return home / ".codex" / "sessions" if home else None
