"""本機小幫手：讓房間畫面看得到、按得到本機其他工具的東西（只在使用者自己的電腦上，綁 127.0.0.1）。

目前接兩樣，都是選配，裝了才出現：
- acc（Agent Context Controller）：點成員的點多一個「acc」鈕，看那個對話的目前目標、擱置、已結案、工作紀錄，
  以及它專案上等使用者決定的 Super 建議。找法：PATH 上的 `acc`（或 ROOMS_ACC_REPO 指到 repo）。
- 判斷層的建議佇列：ROOMS_JUDGMENT_LAYER 指到那個資料夾才接，照它的命令列介面 list／decide。
- 本機工具（畫面右側的小球）：看有沒有在跑、照登記的指令啟動、產生 repo 的程式圖。

核心跑在容器裡碰不到這些，所以由這支在主機上跑、頁面直接連。任何網頁都能連 127.0.0.1，
所以每個請求都要帶 X-Rooms-Local 標頭（逼瀏覽器先問過 CORS），而且只認 ROOMS_LOCAL_ORIGINS 列的來源。

  room local            # 預設 127.0.0.1:8777（ROOMS_LOCAL_PORT）
"""

import functools
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("ROOMS_LOCAL_PORT", "8777"))
ORIGINS = {o.strip().rstrip("/") for o in os.environ.get(
    "ROOMS_LOCAL_ORIGINS", "http://localhost:8775,http://127.0.0.1:8775").split(",") if o.strip()}


@functools.lru_cache(maxsize=1)
def acc():
    """回 (GoalsModule, RapportModule, acc.web.server, root)；沒裝 acc 回 None。"""
    repo = os.environ.get("ROOMS_ACC_REPO", "").strip()
    if not repo:
        exe = shutil.which("acc")
        if not exe:
            return None
        repo = str(Path(os.path.realpath(exe)).parent.parent)       # <repo>/bin/acc
    try:
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from acc import core
        from acc.modules.goals import GoalsModule
        from acc.modules.rapport import RapportModule
        from acc.web import server
        return GoalsModule, RapportModule, server, core.get_root
    except Exception:
        return None


def jl_dir():
    path = os.environ.get("ROOMS_JUDGMENT_LAYER", "").strip()
    return path if path and (Path(path) / "jl" / "suggestions.py").exists() else None


def jl(*args):
    env = dict(os.environ)
    if acc():
        # 接受建議時佇列自己叫 acc（找 PATH 上的 acc）；systemd 的 PATH 常常沒有它，把找到的那個補上
        bin_dir = str(Path(sys.modules["acc"].__file__).resolve().parent.parent / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    out = subprocess.run([sys.executable, "-m", "jl.suggestions", *args], cwd=jl_dir(), env=env,
                         capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip()[-400:] or "判斷層回錯")
    return json.loads(out.stdout)


def caps():
    return {"acc": acc() is not None, "jl": jl_dir() is not None}


def _goals():
    Goals, Rapport, server, root = acc()
    r = root()
    return Goals(r), Rapport(r), server


def session_view(sid):
    """這個對話的 acc 狀態＋跟它有關、等使用者決定的建議。"""
    out = {"acc": None, "log": "", "super": [], "suggestions": []}
    if acc():
        goals, rapport, server = _goals()
        s = goals.load_session(sid)
        if s:
            out["acc"] = server._session_view(goals, rapport, s)
            try:
                out["log"] = goals.work_log(sid)
            except Exception as exc:
                out["log"] = f"（讀不到工作紀錄：{exc}）"
            project = s.get("project") or ""
            if project:
                out["super"] = [dict(x, project=project) for x in goals.suggestions_view(project)
                                if x.get("status") == "pending"]
    if jl_dir():
        out["suggestions"] = jl("list", "--session", sid)
    return out


def badges(sids):
    """{session: 等決定的建議數}：只算這個對話自己的（判斷層的建議）。Super 建議掛在專案上，
    同專案的每個點都掛同一個數字反而看不出誰有事，所以只在 acc 面板裡列。"""
    counts = {sid: 0 for sid in sids}
    if jl_dir():
        for x in jl("list"):
            sid = (x.get("subject") or {}).get("session")
            if sid in counts:
                counts[sid] += 1
    return counts


def decide(data):
    """接受／拒絕一則建議：Super 建議交給 acc，判斷層的交給佇列（它自己執行並記下結果）。"""
    choice = data.get("choice")
    if choice not in ("accept", "reject"):
        raise ValueError("choice 要是 accept 或 reject")
    if data.get("source") == "super":
        if not acc():
            raise ValueError("這台沒有 acc")
        goals, _, _ = _goals()
        fn = goals.accept_suggestion if choice == "accept" else goals.reject_suggestion
        return fn(data["project"], data["id"])
    if not jl_dir():
        raise ValueError("這台沒有判斷層")
    return jl("decide", data["id"], choice, "--by", "user", "--via", "room")


# ── 本機工具：看有沒有在跑、照登記的指令啟動、產生程式圖。工具的資料以核心登記的為準，頁面送來的只拿代號 ──

CORE = os.environ.get("ROOMS_URL", "http://127.0.0.1:8775")


def core_tools():
    from rooms.identity import USER
    with urllib.request.urlopen(f"{CORE}/api/tools?as={USER}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))["result"]


def _tool(tool_id):
    tool = next((t for t in core_tools() if t["id"] == tool_id), None)
    if not tool:
        raise ValueError(f"沒有這個工具：{tool_id}")
    return tool


def _alive(url):
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)), timeout=0.4):
            return True
    except OSError:
        return False


def tool_status():
    return {t["id"]: _alive(t["url"]) for t in core_tools()}


def start_tool(tool_id):
    """照核心登記的啟動指令，在 repo 資料夾裡背景跑起來（關掉頁面也不會停）；輸出寫進記錄檔。"""
    tool = _tool(tool_id)
    if not tool.get("start"):
        raise ValueError(f"{tool['name']} 沒登記怎麼啟動")
    if _alive(tool["url"]):
        return {"id": tool_id, "already": True}
    logs = Path(os.path.expanduser("~/.local/state/agent-rooms/tools"))
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / f"{tool_id}.log", "ab")
    subprocess.Popen(["bash", "-lc", tool["start"]], cwd=os.path.expanduser(tool.get("repo") or "~"),
                     stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    return {"id": tool_id, "started": True, "log": str(logs / f"{tool_id}.log")}


def _listeners(url):
    """在聽這個網址那個埠的程式（pid）。用 ss 找，不管是不是房間開的（例如別的對話開的也停得掉）。"""
    port = urlparse(url).port
    if not port:
        return []
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=5).stdout
    return sorted({int(p) for p in __import__("re").findall(r"pid=(\d+)", out)})


def stop_tool(tool_id):
    """停掉在聽這個工具埠的程式（先禮貌地請它停，兩秒還在就強制）。轉進來的網址（例如 ssh 轉發）不停。"""
    import signal
    import time
    tool = _tool(tool_id)
    if not tool.get("start"):
        raise ValueError(f"{tool['name']} 不是在這台跑的（沒登記啟動指令），不能從這裡停")
    pids = _listeners(tool["url"])
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        if not _listeners(tool["url"]):
            break
        time.sleep(0.1)
    for pid in _listeners(tool["url"]):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return {"id": tool_id, "stopped": pids}


def restart_tool(tool_id):
    stop_tool(tool_id)
    import time
    for _ in range(30):
        if not _alive(_tool(tool_id)["url"]):
            break
        time.sleep(0.1)
    return start_tool(tool_id)


def tool_map(tool_id):
    from rooms import codemap
    tool = _tool(tool_id)
    if not tool.get("repo"):
        raise ValueError(f"{tool['name']} 沒登記 repo")
    path, text = codemap.write(tool_id, os.path.expanduser(tool["repo"]))
    return {"id": tool_id, "path": str(path), "text": text}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _origin(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        return origin if origin in ORIGINS else None

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self._origin()
        self.send_response(204 if origin else 403)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Rooms-Local")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
        self.end_headers()

    def _allowed(self):
        # 沒帶標頭（例如別的網頁偷送的簡單請求）或來源不在名單：一律擋
        if self.headers.get("X-Rooms-Local") != "1" or not self._origin():
            self._send({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/local/caps":
                return self._send(caps())
            if url.path == "/local/session":
                return self._send(session_view(q.get("sid", "")))
            if url.path == "/local/tools/status":
                return self._send(tool_status())
            if url.path == "/local/tools/map":
                return self._send(tool_map(q.get("id", "")))
            if url.path == "/local/badges":
                return self._send(badges([s for s in q.get("sids", "").split(",") if s]))
            self._send({"error": "not found"}, 404)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        if not self._allowed():
            return
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if urlparse(self.path).path == "/local/decide":
                return self._send({"ok": True, "result": decide(data)})
            if urlparse(self.path).path == "/local/tools/start":
                return self._send(start_tool(data.get("id", "")))
            if urlparse(self.path).path == "/local/tools/stop":
                return self._send(stop_tool(data.get("id", "")))
            if urlparse(self.path).path == "/local/tools/restart":
                return self._send(restart_tool(data.get("id", "")))
            self._send({"error": "not found"}, 404)
        except (ValueError, KeyError) as exc:
            self._send({"error": str(exc)}, 400)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(port=PORT):
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
