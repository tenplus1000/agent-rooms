"""帳號額度：這台機器上有哪些 Claude／Codex 帳號、各自的 5 小時與 7 天用量。

帳號清單存在 ~/.config/agent-rooms/accounts.json（每台機器自己的，畫面上可以加、改、刪）。
沒有這個檔時自動找：~/.claude 與 ~/.claude-*（有登入檔的）、~/.codex 與 ~/.codex-*；
ROOMS_CLAUDE_HOMES／ROOMS_CODEX_HOMES 裡取的名字優先用。

用量只讀、不叫模型：
- Claude：拿那個帳號登入檔裡的 token 問 api.anthropic.com/api/oauth/usage。登入檔裡的通行碼大約 8 小時到期，
  帳號閒著就不會換新（登入本身沒過期）：到期了先跑一次 `claude auth status` 讓 claude 自己換新，不動登入檔。
- Codex：開那個帳號的 `codex app-server` 問 account/rateLimits/read。
結果存在 ~/.cache/agent-rooms/usage.json，所有程式共用（畫面、命令列、轉送程式不會各問各的）；
Claude 10 分鐘、Codex 5 分鐘內不重問。用量網址查太頻繁會回 429：這時照舊顯示上次查到的數字並註明時間，
隔 10、20、40… 分鐘（最多 1 小時）再試。token 只在這台機器上用，不印、不存。
"""
import fcntl
import json
import os
import select
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("ROOMS_ACCOUNTS_FILE", "").strip() or os.path.expanduser("~/.config/agent-rooms/accounts.json"))
WARN = 80            # 預設提醒線（%）：5 小時額度到這裡就提醒先停
CACHE_SECONDS = {"claude": 600, "codex": 300}
BACKOFF = 600         # 被限流後第一次等多久再試，之後加倍
BACKOFF_MAX = 3600
CACHE = Path(os.path.expanduser("~/.cache/agent-rooms/usage.json"))
_lock = threading.Lock()


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


def _claude_creds(home):
    return json.loads((Path(home) / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]


def _claude_refresh(home):
    """通行碼到期：讓 claude 自己換新（`claude auth status` 會順手換，不耗額度），我們不寫登入檔。"""
    exe = os.environ.get("ROOMS_CLAUDE_BIN", "").strip() or "claude"
    env = dict(os.environ)
    if os.path.realpath(home) != os.path.realpath(os.path.expanduser("~/.claude")):
        env["CLAUDE_CONFIG_DIR"] = home
    try:
        subprocess.run([exe, "auth", "status"], env=env, capture_output=True, timeout=60)
    except Exception:
        pass


def _claude(home):
    try:
        creds = _claude_creds(home)
    except (OSError, ValueError, KeyError):
        return {"error": "這個帳號沒有登入（找不到登入檔）"}
    if creds.get("expiresAt") and creds["expiresAt"] / 1000 < time.time() + 60:
        _claude_refresh(home)
        try:
            creds = _claude_creds(home)
        except (OSError, ValueError, KeyError):
            return {"error": "這個帳號沒有登入（找不到登入檔）"}
        if creds.get("expiresAt") and creds["expiresAt"] / 1000 < time.time():
            return {"error": "通行碼沒換新成功：用這個帳號開一次 claude 看看是不是要重新登入", "plan": creds.get("subscriptionType")}

    def ask(token):
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20", "User-Agent": "agent-rooms"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    try:
        try:
            got = ask(creds["accessToken"])
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            _claude_refresh(home)          # 時間還沒到就被拒：一樣讓 claude 換新再問一次
            got = ask(_claude_creds(home)["accessToken"])
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {"error": "查太頻繁，被用量網址擋了", "limited": True, "plan": creds.get("subscriptionType")}
        return {"error": f"問不到用量：HTTP {exc.code}", "plan": creds.get("subscriptionType")}
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


def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_entry(key, entry):
    with _lock, open(str(CACHE) + ".lock", "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        cache = _load()
        cache[key] = entry
        tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE)


def usage(account, force=False):
    """{five_hours: {used, resets_at}, week: {...}, plan, checked} 或 {error}。

    查不到時（被限流、暫時連不上）有上次查到的就回那份，加上 stale（說明是幾點的、為什麼）。
    force（畫面的 ↻）不等快取時間，但被限流的等待照樣要等。
    """
    key, kind = account["home"], account.get("kind", "claude")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    slot = CACHE.parent / ("usage-" + "".join(c if c.isalnum() else "_" for c in key) + ".lock")
    with open(slot, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)    # 同一個帳號同時只有一個程式在問；其他人等它問完讀同一份
        entry = _load().get(key) or {}
        good, now = entry.get("good"), time.time()

        def from_good(why):
            return dict(good, stale=f"{why}；這是 {_hhmm(good['checked'])} 查到的") if good else {"error": why, "checked": now}
        if good and not force and now - good["checked"] < CACHE_SECONDS.get(kind, 300):
            return good
        if now < entry.get("retry_at", 0):
            return from_good(f"查太頻繁被擋，{_hhmm(entry['retry_at'])} 再查")
        out = _claude(key) if kind == "claude" else _codex(key)
        out["checked"] = now
        if not out.get("error"):
            entry, result = {"good": out}, out
        elif out.get("limited"):
            fails = entry.get("fails", 0) + 1
            entry = dict(entry, fails=fails, retry_at=now + min(BACKOFF_MAX, BACKOFF * 2 ** (fails - 1)))
            result = from_good(f"查太頻繁被擋，{_hhmm(entry['retry_at'])} 再查")
        else:
            result = from_good(out["error"]) if good else out
        _save_entry(key, entry)
        return result


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
    note = f"（{u['stale']}）" if u.get("stale") else ""

    def part(label, w):
        if not w or w.get("used") is None:
            return f"{label} ?"
        at = when(w.get("resets_at"))
        return f"{label} {w['used']:.0f}%" + (f"（{at} 重置）" if at else "")
    flag = "　⚠ 到提醒線了" if ((u.get("five_hours") or {}).get("used") or 0) >= row.get("warn", WARN) else ""
    return f"{row['name']}（{row['kind']}）：{part('5 小時', u.get('five_hours'))}、{part('7 天', u.get('week'))}{flag}{note}"


if __name__ == "__main__":
    for r in all_usage(force=True):
        print(line(r))
