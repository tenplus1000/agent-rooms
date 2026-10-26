#!/usr/bin/env python3
"""把房間 hook 補進這台機器上每個 Claude 帳號的 settings.json（已經有的不重複加；改之前備份成 .bak）。
用法：python3 add-room-hooks.py               # 全部帳號：~/.claude 與 ~/.claude-*（有登入過的）
      python3 add-room-hooks.py <settings.json>  # 只改這一份"""
import json, os, shutil, sys
from pathlib import Path

home = Path.home()
targets = [Path(sys.argv[1])] if len(sys.argv) > 1 else [
    d / "settings.json" for d in [home / ".claude"] + sorted(home.glob(".claude-*"))
    if d.is_dir() and ((d / ".credentials.json").exists() or (d / "settings.json").exists())]
for path in targets:
    cmd = str(Path(__file__).resolve().parent.parent / "bin" / "room") + " hook"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    want = {"Stop": {"type": "command", "command": cmd, "timeout": 86400, "asyncRewake": True},
            "UserPromptSubmit": {"type": "command", "command": cmd, "timeout": 5},
            "PostToolUse": {"type": "command", "command": cmd, "timeout": 5}}
    added = []
    for event, hook in want.items():
        groups = hooks.setdefault(event, [])
        if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
            continue
        plain = next((g for g in groups if not g.get("matcher")), None)
        if plain is None:
            groups.append({"hooks": [hook]})
        else:
            plain.setdefault("hooks", []).append(hook)
        added.append(event)
    if added:
        if path.exists():
            shutil.copy2(path, str(path) + ".bak")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{path}：" + (f"加了 {'、'.join(added)}（舊檔備份在 {path}.bak）" if added else "房間 hook 都已經在了"))
