#!/usr/bin/env python3
"""把房間接上這台機器上的每一個 Claude／Codex（已經接好的不重複加；改之前備份成 .bak）。

  python3 add-room-hooks.py

- WSL 的 Claude：~/.claude 與 ~/.claude-*（有登入過或有設定檔的帳號），補上 UserPromptSubmit、PostToolUse、Stop、SessionStart 四個 hook。
- Windows 的 Claude（桌面版、Windows 終端機）：%USERPROFILE%\\.claude\\settings.json。hook 要經 wsl.exe 轉進來，
  照那份檔裡已經有的房間 hook 指令（win_bridge.py 那行）補齊缺的；一行都沒有就跳過並說明。
- Codex：~/.codex 與 ~/.codex-* 的 config.toml 沒有 [mcp_servers.agent-rooms] 就補上。
  Windows 桌面版 Codex 用 wsl.exe 跑房間指令回話，不用另外設定。
"""
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from rooms import paths  # noqa: E402

CMD = f"{REPO / 'bin' / 'room'} hook"
EVENTS = {"UserPromptSubmit": {"timeout": 5}, "PostToolUse": {"timeout": 5},
          "Stop": {"timeout": 86400, "asyncRewake": True}, "SessionStart": {"timeout": 86400, "asyncRewake": True}}
home = Path.home()


def backup_write(path, text):
    if path.exists():
        shutil.copy2(path, str(path) + ".bak")
    path.write_text(text, encoding="utf-8")


def add_hooks(path, cmd):
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    added = []
    for event, extra in EVENTS.items():
        groups = hooks.setdefault(event, [])
        if any("room" in h.get("command", "") and " hook" in h.get("command", "")
               and "agent-rooms" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        hook = {"type": "command", "command": cmd, **extra}
        plain = next((g for g in groups if not g.get("matcher")), None)
        if plain is None:
            groups.append({"hooks": [hook]})
        else:
            plain.setdefault("hooks", []).append(hook)
        added.append(event)
    if added:
        backup_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"{path}：" + (f"加了 {'、'.join(added)}" if added else "房間 hook 都已經在了"))


# WSL 的 Claude 帳號
for d in [home / ".claude"] + sorted(home.glob(".claude-*")):
    if d.is_dir() and ((d / ".credentials.json").exists() or (d / "settings.json").exists()):
        add_hooks(d / "settings.json", CMD)

# Windows 的 Claude
win = paths.windows_home()
if win and (win / ".claude").is_dir():
    path = win / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    bridge = next((h["command"] for v in (data.get("hooks") or {}).values() for g in v for h in g.get("hooks", [])
                   if "wsl.exe" in h.get("command", "") and CMD in h.get("command", "")), None)
    if bridge:
        add_hooks(path, bridge)
    else:
        print(f"{path}：沒有現成的 wsl.exe 房間 hook 可以照抄，跳過（做法見 README「Windows 上的 Claude」）")

# Codex（WSL）
block = (f'\n[mcp_servers.agent-rooms]\ncommand = "python3"\nargs = ["{REPO / "adapters" / "mcp" / "server.py"}"]\n'
         'default_tools_approval_mode = "approve"\n\n[mcp_servers.agent-rooms.env]\nROOMS_URL = "http://127.0.0.1:8775"\n')
seen = set()
for d in [home / ".codex"] + sorted(home.glob(".codex-*")):
    cfg = d / "config.toml"
    if not cfg.exists() or os.path.realpath(cfg) in seen:
        continue
    seen.add(os.path.realpath(cfg))
    text = cfg.read_text(encoding="utf-8")
    if "[mcp_servers.agent-rooms]" in text:
        print(f"{cfg}：房間工具已經在了")
    else:
        backup_write(Path(os.path.realpath(cfg)), text + block)
        print(f"{cfg}：加了房間工具（MCP）")
