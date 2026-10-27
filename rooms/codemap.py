"""程式圖：一個 repo 裡每個程式檔在做什麼、用到同 repo 的哪些檔，一頁看完（給人和 agent 讀程式前先看）。

  python3 -m rooms.codemap <repo 路徑>

只讀檔案、不跑程式。每個檔取開頭的說明（Python 的 docstring，或其他語言開頭的註解），
Python 另外列出它 import 了同 repo 的哪些模組。
"""
import ast
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "data", ".mypy_cache", ".pytest_cache"}
CODE = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".toml", ".json"}
LIMIT = 200


def _purpose_py(text):
    try:
        doc = ast.get_docstring(ast.parse(text)) or ""
    except SyntaxError:
        doc = ""
    return doc.strip().splitlines()[0] if doc.strip() else _purpose_comment(text)


def _purpose_comment(text):
    for line in text.splitlines()[:15]:
        m = re.match(r"\s*(?:#|//|/\*+|\*|<!--)\s*(.+?)\s*(?:\*/|-->)?\s*$", line)
        if m and not m.group(1).startswith(("!", "-*-", "eslint", "@ts")) and len(m.group(1)) > 3:
            return m.group(1)
    m = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
    return m.group(1).strip() if m else ""


def _local_imports(text, modules):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    used = set()
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module or ""] + [f"{node.module}.{a.name}" if node.module else a.name for a in node.names] \
            if isinstance(node, ast.ImportFrom) else []
        for name in names:
            for mod in modules:
                if name == mod or name.endswith("." + mod) or mod.endswith("." + name) and name:
                    used.add(mod)
    return sorted(used)


def build(repo):
    repo = Path(repo).expanduser().resolve()
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            path = Path(root) / name
            if path.suffix in CODE and path.stat().st_size < 2_000_000 and not name.endswith((".lock", "-lock.json")):
                files.append(path)
    shown = files[:LIMIT]
    modules = {".".join(p.relative_to(repo).with_suffix("").parts): p for p in files if p.suffix == ".py"}
    short = {m: m.split(".")[-1] for m in modules}
    lines = [f"# 程式圖：{repo.name}", "", f"`{repo}`，{len(files)} 個程式檔" + (f"（只列前 {LIMIT} 個）" if len(files) > LIMIT else ""), ""]
    current = None
    for path in shown:
        rel = path.relative_to(repo)
        folder = str(rel.parent) if str(rel.parent) != "." else "（根目錄）"
        if folder != current:
            lines += ["", f"## {folder}"]
            current = folder
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        purpose = _purpose_py(text) if path.suffix == ".py" else _purpose_comment(text)
        row = f"- **{rel.name}**（{text.count(chr(10)) + 1} 行）" + (f"：{purpose[:140]}" if purpose else "")
        if path.suffix == ".py":
            uses = [u for u in _local_imports(text, list(modules) + list(short.values()))
                    if u in modules and modules[u] != path]
            if uses:
                row += f"　→ 用到 {', '.join(sorted({u.split('.')[-1] for u in uses}))}"
        lines.append(row)
    return "\n".join(lines) + "\n"


def cache_path(tool_id):
    return Path(os.path.expanduser("~/.local/state/agent-rooms/maps")) / f"{tool_id}.md"


def write(tool_id, repo):
    """產生並存起來（agent 讀這個檔），回 (路徑, 內容)。"""
    text = build(repo)
    path = cache_path(tool_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text


if __name__ == "__main__":
    sys.stdout.write(build(sys.argv[1] if len(sys.argv) > 1 else "."))
