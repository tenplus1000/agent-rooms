"""一個開關換掉所有專案用的 OpenRouter key。

設定都在這台機器自己的資料夾（ROOMS_KEYS_DIR，預設 ~/.config/agent-rooms/openrouter），不進 repo：
  key1、key2     兩把 key（權限 600）
  targets.json   {"hosts": {名字: {"via": [...ssh...], "scan": [資料夾...]}},
                  "targets": [{id, project, host, path, mode, restart, verify, note}],
                  "ignore": [{"match": 路徑正規式, "why": 為什麼不切}]}
  state.json     目前切到哪一把、哪些機器還沒補上
對外（頁面）一律只有尾三碼。關機的機器記成待補，連得上時自動補。
"""
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()
_status = {"at": 0, "rows": {}}
_scan_lock = threading.Lock()
RETRY_EVERY = 300
SCAN_EVERY = 86400


def base():
    return os.path.expanduser(os.environ.get("ROOMS_KEYS_DIR", "~/.config/agent-rooms/openrouter"))


def _path(name):
    return os.path.join(base(), name)


def _read_json(name, default):
    try:
        with open(_path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(name, data):
    tmp = _path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(name))


def keys():
    out = {}
    for slot in ("1", "2"):
        try:
            with open(_path("key" + slot), encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            value = ""
        if value:
            out[slot] = value
    return out


def configured():
    return len(keys()) == 2 and os.path.exists(_path("targets.json"))


def _config():
    return _read_json("targets.json", {"hosts": {}, "targets": [], "ignore": []})


def _state():
    return _read_json("state.json", {"active": None, "pending": []})


def _run_on(host, module, payload, timeout):
    """把 module 加上參數經 stdin 送到那台機器跑；key 不會出現在任何命令列。"""
    via = (_config().get("hosts", {}).get(host) or {}).get("via") or []
    with open(os.path.join(HERE, module), encoding="utf-8") as fh:
        source = fh.read()
    program = f"{source}\nimport base64 as _b\nmain(json.loads(_b.b64decode('" + \
        __import__("base64").b64encode(json.dumps(payload).encode()).decode() + "')))\n"
    got = subprocess.run([*via, "python3", "-"], input=program, capture_output=True, text=True, timeout=timeout)
    if got.returncode:
        raise ConnectionError((got.stderr or "連不上").strip().splitlines()[-1][:120] if got.stderr.strip() else "連不上")
    return json.loads(got.stdout.strip().splitlines()[-1])


def _by_host(targets):
    groups = {}
    for target in targets:
        groups.setdefault(target.get("host") or "", []).append(target)
    return groups


def _collect(op, targets, active=None):
    known = list(keys().values())
    rows = {}

    def one(item):
        host, group = item
        payload = {"op": op, "keys": known, "active": active, "targets": group}
        try:
            for row in _run_on(host, "keyagent.py", payload, 240 if op == "apply" else 25):
                rows[row["id"]] = row
            return host, True
        except Exception as exc:
            message = str(exc)[:120] or type(exc).__name__
            for target in group:
                rows[target["id"]] = {"id": target["id"], "offline": True, "tails": [], "problems": [message]}
            return host, False

    groups = _by_host(targets)
    with ThreadPoolExecutor(max_workers=max(1, len(groups))) as pool:
        reached = dict(pool.map(one, groups.items()))
    return rows, reached


def _public_rows(rows):
    conf, active = _config(), (keys().get(str(_state().get("active"))) or "")
    want = active[-3:]
    out = []
    for target in conf.get("targets", []):
        row = rows.get(target["id"]) or {}
        tails = row.get("tails") or []
        out.append({"id": target["id"], "project": target.get("project", ""), "host": target.get("host") or "這台",
                    "path": target.get("path", ""), "note": target.get("note", ""), "tails": tails,
                    "offline": bool(row.get("offline")), "problems": row.get("problems") or [],
                    "error": row.get("error", ""), "ok": bool(want) and tails == [want] and not row.get("error")})
    return out


def view(check=False):
    """給頁面看的：check=True 才真的連到各機器查（比較慢）。"""
    if not configured():
        return {"configured": False}
    state, known = _state(), keys()
    if check:
        rows, _ = _collect("status", _config().get("targets", []))
        _status.update(at=time.time(), rows=rows)
    scan = _read_json("scan.json", {})
    return {"configured": True, "active": state.get("active"), "pending": state.get("pending", []),
            "switched_at": state.get("switched_at"), "keys": [{"slot": s, "tail": v[-3:]} for s, v in sorted(known.items())],
            "targets": _public_rows(_status["rows"]), "checked": _status["at"], "scan": scan}


def _unfinished(targets, rows, reached):
    """還要再試的機器：連不上的，或有服務這次沒換成（例如有人正在用、先不重啟）。"""
    pending = {h for h, ok in reached.items() if not ok}
    pending |= {t.get("host") or "" for t in targets if (rows.get(t["id"]) or {}).get("error")}
    return sorted(pending)


def switch(slot):
    slot = str(slot)
    known = keys()
    if slot not in known:
        raise ValueError("沒有這把 key")
    with _lock:
        targets = _config().get("targets", [])
        state = _state()
        state.update(active=slot, switched_at=time.time(), pending=[])
        _write_json("state.json", state)
        rows, reached = _collect("apply", targets, known[slot])
        state["pending"] = _unfinished(targets, rows, reached)
        _write_json("state.json", state)
        _status.update(at=time.time(), rows=rows)
    return view()


def retry_pending():
    """關機的機器一連得上就補切。"""
    if not configured():
        return
    with _lock:
        state = _state()
        pending, known = state.get("pending") or [], keys()
        if not pending or str(state.get("active")) not in known:
            return
        targets = [t for t in _config().get("targets", []) if (t.get("host") or "") in pending]
        rows, reached = _collect("apply", targets, known[str(state["active"])])
        state["pending"] = _unfinished(targets, rows, reached)
        _write_json("state.json", state)
        _status["rows"].update(rows)


def _outside(host, found, conf, tails):
    """掃到的、認得的 key，不在名單也不在略過清單裡的。"""
    listed = {os.path.expanduser(t["path"]) for t in conf.get("targets", [])
              if (t.get("host") or "") == host and t.get("path")}
    listed |= {t["path"] for t in conf.get("targets", []) if (t.get("host") or "") == host and t.get("path")}
    docker = {(t.get("verify") or {}).get("docker") for t in conf.get("targets", []) if (t.get("host") or "") == host}
    ignore = conf.get("ignore", [])

    def why(text):
        return next((rule["why"] for rule in ignore if re.search(rule["match"], text)), None)

    outside, skipped = [], 0
    store = base() + os.sep                    # 開關自己放 key 的地方
    for path, ft in found["files"].items():
        if path.startswith(store) or not set(ft) & tails or path in listed or any(path.endswith(p.lstrip("~")) for p in listed):
            continue
        if why(path):
            skipped += 1
        else:
            outside.append({"host": host or "這台", "where": path, "tails": ft})
    for item in found["containers"]:
        label = f"容器 {item['name']} 的 {item['var']}"
        if item["tail"] in tails and item["name"] not in docker:
            if why(label):
                skipped += 1
            else:
                outside.append({"host": host or "這台", "where": label, "tails": [item["tail"]]})
    return outside, skipped


def scan():
    """每台機器掃一次，列出名單外的地方。只讀；可能要幾分鐘。"""
    if not configured() or not _scan_lock.acquire(blocking=False):
        return _read_json("scan.json", {})
    try:
        conf = _config()
        tails = {v[-3:] for v in keys().values()}
        result = {"at": time.time(), "outside": [], "skipped": 0, "offline": [], "scanned": 0, "procs": {}}

        def one(item):
            host, spec = item
            return host, _run_on(host, "keyscan.py", {"roots": spec.get("scan") or ["~"]}, 1800)

        hosts = conf.get("hosts", {})
        with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
            futures = {h: pool.submit(one, (h, s)) for h, s in hosts.items()}
        for host, future in futures.items():
            try:
                _, found = future.result()
            except Exception:
                result["offline"].append(host or "這台")
                continue
            outside, skipped = _outside(host, found, conf, tails)
            result["outside"] += outside
            result["skipped"] += skipped
            result["scanned"] += found["scanned"]
            result["procs"][host or "這台"] = {t: n for t, n in found["procs"].items() if t in tails}
        _write_json("scan.json", result)
        return result
    finally:
        _scan_lock.release()


def background():
    """補切關機的機器；每天掃一次有沒有新的地方用到 key。"""
    def loop():
        while True:
            time.sleep(RETRY_EVERY)
            try:
                retry_pending()
                if time.time() - (_read_json("scan.json", {}).get("at") or 0) > SCAN_EVERY:
                    scan()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()
