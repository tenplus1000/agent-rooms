"""帳號額度：這台機器上有哪些 Claude／Codex 帳號、各自的 5 小時與 7 天用量。

帳號清單存在 ~/.config/agent-rooms/accounts.json（每台機器自己的，畫面上可以加、改、刪）。
沒有這個檔時自動找：~/.claude 與 ~/.claude-*（有登入檔的）、~/.codex 與 ~/.codex-*；
ROOMS_CLAUDE_HOMES／ROOMS_CODEX_HOMES 裡取的名字優先用。

用量只讀、不叫模型：
- Claude：拿那個帳號登入檔裡的 token 問 api.anthropic.com/api/oauth/usage。
- Codex：開那個帳號的 `codex app-server` 問 account/rateLimits/read。
結果快取 5 分鐘。token 只在這台機器上用，不印、不存。
"""
import json
import os
import select
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("ROOMS_ACCOUNTS_FILE", "").strip() or os.path.expanduser("~/.config/agent-rooms/accounts.json"))
WARN = 80            # 預設提醒線（%）：使用者 的原則「5 小時額度到 80% 要停」
CACHE_SECONDS = 300
_lock = threading.Lock()
_cache = {}


def _env_names(var, default):
    names = {}
    for entry in (os.environ.get(var, "").strip() or default).split(":"):
        name, sep, path = entry.strip().partition("=")
        if not sep:
            name, path = "", name
        if path:
            names[os.path.realpath(os.path.expanduser(path))] = name
    return names


def detect():
    """沒設定過時，這台機器上找得到的帳號（每個人不一樣）。"""
    home = Path.home()
    found = []
    claude_names = _env_names("ROOMS_CLAUDE_HOMES", "~/.claude")
    codex_names = _env_names("ROOMS_CODEX_HOMES", "~/.codex")
    seen = set()
    for kind, base, login, names in (("claude", ".claude", ".credentials.json", claude_names),
                                     ("codex", ".codex", "auth.json", codex_names)):
        for d in [home / base] + sorted(home.glob(base + "-*")):
            real = os.path.realpath(d)
            if real in seen or not (d / login).is_file():
                continue
            seen.add(real)
            name = names.get(real) or (kind if d.name == base else d.name.lstrip("."))
            found.append({"name": name, "kind": kind, "home": str(d), "warn": WARN})
    return found


def accounts():
    try:
        rows = json.loads(CONFIG.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            return rows
    except (OSError, ValueError):
        pass
    return detect()


def save(rows):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG)


def upsert(row):
    """加或改一個帳號（照名字）。"""
    name = (row.get("name") or "").strip()
    kind = row.get("kind")
    home = os.path.expanduser((row.get("home") or "").strip())
    if not name or kind not in ("claude", "codex") or not home:
        raise ValueError("帳號要有名字、種類（claude／codex）和資料夾")
    rows = [r for r in accounts() if r["name"] != name]
    warn = row.get("warn")
    rows.append({"name": name, "kind": kind, "home": home, "warn": int(warn) if str(warn or "").strip() else WARN})
    save(rows)
    return rows


def remove(name):
    rows = [r for r in accounts() if r["name"] != name]
    save(rows)
    return rows


def _claude(home):
    try:
        creds = json.loads((Path(home) / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
    except (OSError, ValueError, KeyError):
        return {"error": "這個帳號沒有登入（找不到登入檔）"}
    if creds.get("expiresAt") and creds["expiresAt"] / 1000 < time.time():
        return {"error": "登入過期了：用這個帳號開一次 claude 就會更新", "plan": creds.get("subscriptionType")}
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + creds["accessToken"], "anthropic-beta": "oauth-2025-04-20", "User-Agent": "agent-rooms"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            got = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"error": f"問不到用量：{type(exc).__name__}", "plan": creds.get("subscriptionType")}

    def win(w):
        return {"used": w.get("utilization"), "resets_at": w.get("resets_at")} if isinstance(w, dict) else None
    return {"five_hours": win(got.get("five_hour")), "week": win(got.get("seven_day")), "plan": creds.get("subscriptionType")}


def _codex(home, timeout=20):
    if not (Path(home) / "auth.json").is_file():
        return {"error": "這個帳號沒有登入（找不到登入檔）"}
    managed = Path(home) / "packages" / "app-server-daemon" / "current" / "bin" / "codex"
    exe = os.environ.get("ROOMS_CODEX_BIN", "").strip() or (str(managed) if managed.exists() else "codex")
    try:
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=dict(os.environ, CODEX_HOME=home), text=True)
    except OSError as exc:
        return {"error": f"開不了 codex：{exc}"}
    try:
        def send(obj):
            p.stdin.write(json.dumps(obj) + "\n")
            p.stdin.flush()
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-rooms", "version": "1"}}})
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([p.stdout], [], [], end - time.time())
            if not ready:
                break
            line = p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != 2:
                continue
            if "error" in msg:
                return {"error": str(msg["error"].get("message") or msg["error"])[:200]}
            rl = (msg.get("result") or {}).get("rateLimits") or {}

            def win(w):
                if not isinstance(w, dict):
                    return None
                at = w.get("resetsAt")
                return {"used": w.get("usedPercent"), "mins": w.get("windowDurationMins"),
                        "resets_at": __import__("datetime").datetime.fromtimestamp(at).astimezone().isoformat() if isinstance(at, (int, float)) else at}
            windows = [w for w in (win(rl.get("primary")), win(rl.get("secondary"))) if w]
            return {"five_hours": next((w for w in windows if (w["mins"] or 0) == 300), None),
                    "week": next((w for w in windows if (w["mins"] or 0) >= 1440 * 6), None), "plan": rl.get("planType")}
        return {"error": "codex 沒有回應"}
    finally:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def usage(account, force=False):
    """{five_hours: {used, resets_at}, week: {...}, plan} 或 {error}；都帶 checked。"""
    key = account["home"]
    with _lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit["checked"] < CACHE_SECONDS:
            return hit
    out = _claude(account["home"]) if account["kind"] == "claude" else _codex(account["home"])
    out["checked"] = time.time()
    with _lock:
        _cache[key] = out
    return out


def all_usage(force=False):
    rows = []
    threads = []
    for acc in accounts():
        row = dict(acc)
        rows.append(row)
        t = threading.Thread(target=lambda r=row: r.update(usage=usage(r, force)), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=30)
    return rows


def when(iso):
    """重置時間換成這台機器的時區：今天的只寫幾點，其他寫月日。"""
    import datetime
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    return t.strftime("%H:%M") if t.date() == datetime.date.today() else t.strftime("%m-%d %H:%M")


def line(row):
    """給命令列看的一行。"""
    u = row.get("usage") or {}
    if u.get("error"):
        return f"{row['name']}（{row['kind']}）：{u['error']}"

    def part(label, w):
        if not w or w.get("used") is None:
            return f"{label} ?"
        at = when(w.get("resets_at"))
        return f"{label} {w['used']:.0f}%" + (f"（{at} 重置）" if at else "")
    flag = "　⚠ 到提醒線了" if ((u.get("five_hours") or {}).get("used") or 0) >= row.get("warn", WARN) else ""
    return f"{row['name']}（{row['kind']}）：{part('5 小時', u.get('five_hours'))}、{part('7 天', u.get('week'))}{flag}"


if __name__ == "__main__":
    for r in all_usage(force=True):
        print(line(r))
