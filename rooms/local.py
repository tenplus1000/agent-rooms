"""本機小幫手：讓房間畫面看得到、按得到本機其他工具的東西（只在使用者自己的電腦上，綁 127.0.0.1）。

目前接兩樣，都是選配，裝了才出現：
- acc（Agent Context Controller）：點成員的點多一個「acc」鈕，看那個對話的目前目標、擱置、已結案、工作紀錄，
  以及它專案上等使用者決定的 Super 建議。找法：PATH 上的 `acc`（或 ROOMS_ACC_REPO 指到 repo）。
- 判斷層的建議佇列：ROOMS_JUDGMENT_LAYER 指到那個資料夾才接，照它的命令列介面 list／decide。

核心跑在容器裡碰不到這些，所以由這支在主機上跑、頁面直接連。任何網頁都能連 127.0.0.1，
所以每個請求都要帶 X-Rooms-Local 標頭（逼瀏覽器先問過 CORS），而且只認 ROOMS_LOCAL_ORIGINS 列的來源。

  room local            # 預設 127.0.0.1:8777（ROOMS_LOCAL_PORT）
"""

import functools
import json
import os
import shutil
import subprocess
import sys
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
    out = subprocess.run([sys.executable, "-m", "jl.suggestions", *args], cwd=jl_dir(),
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
    """{session: 等決定的建議數}：Super 建議算在專案上（同專案的每個對話都看得到），判斷層的照對話。"""
    counts = {sid: 0 for sid in sids}
    if acc():
        goals, _, _ = _goals()
        per_project = {}
        for sid in sids:
            s = goals.load_session(sid)
            project = (s or {}).get("project")
            if project:
                if project not in per_project:
                    per_project[project] = goals.pending_suggestion_count(project)
                counts[sid] += per_project[project]
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
            self._send({"error": "not found"}, 404)
        except (ValueError, KeyError) as exc:
            self._send({"error": str(exc)}, 400)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(port=PORT):
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
