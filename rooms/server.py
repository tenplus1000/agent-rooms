"""房間核心的 HTTP JSON API，只用標準函式庫。

兩個入口：
  本機（ROOMS_PORT，預設 8775）：身分放在 `as`（query 或 JSON body），agent 填自己的 session id、使用者填
    identity.USER。不驗證，只給同一台電腦上的程式用。
  遠端（ROOMS_REMOTE_PORT，預設 8776）：一律要 `Authorization: Bearer <鑰匙>`，身分由鑰匙決定，`as` 不算數。
    給從網路轉進來的請求用（例如 tailscale serve）；轉進來的請求看起來也是 127.0.0.1，所以用另一個埠分開，
    不靠來源位址判斷。鑰匙用 `room key` 在本機發。
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rooms.core import USER, Core, RoomError

# 遠端的人代表自己時才能用的入口：session 參數必須是它自己（使用者例外）
SELF_ONLY = ("/api/listen", "/api/ack", "/api/touch", "/api/register", "/api/user_here", "/api/delivery")
LOCAL_ONLY = ("/api/keys", "/api/global")         # 發鑰匙、指定全域角色只在本機做

INDEX_HTML = Path(__file__).parent / "web" / "index.html"

TASK_ACTIONS = {
    "claim": lambda c, a, t, b: c.claim(a, t),
    "submit": lambda c, a, t, b: c.submit(a, t, b.get("note", "")),
    "close": lambda c, a, t, b: c.close_task(a, t, b.get("result", ""), b.get("note", "")),
    "park": lambda c, a, t, b: c.park_task(a, t, b.get("reason", "")),
    "revise": lambda c, a, t, b: c.revise_task(a, t, b.get("objective", ""), b.get("criteria", ""), b.get("reason", "")),
    "drop": lambda c, a, t, b: c.drop_task(a, t, b.get("reason", "")),
    "amend": lambda c, a, t, b: c.amend_task(a, t, b.get("to", ""), b.get("reason", "")),
}


def make_handler(core, remote=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安靜；錯誤照樣回給呼叫端
            pass

        def _send(self, status, payload, content_type="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _dispatch(self, method):
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                body = self._body() if method in ("POST", "PUT") else {}
                args = {**query, **body}
                actor = args.get("as", "")
                if remote and url.path.startswith("/api/"):
                    actor = self._remote_actor(url.path, args)
                result = self._route(method, url.path, args, actor)
                if isinstance(result, tuple):
                    self._send(200, result[0], result[1])
                else:
                    self._send(200, {"ok": True, "result": result})
            except RoomError as error:
                self._send(error.status, {"ok": False, "error": str(error)})
            except (ValueError, KeyError) as error:
                self._send(400, {"ok": False, "error": f"參數不對：{error}"})

        def _remote_actor(self, path, args):
            header = self.headers.get("Authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            actor = core.identity_for_key(token)
            if not actor:
                raise RoomError("要鑰匙：Authorization: Bearer <鑰匙>", 401)
            if path.startswith(LOCAL_ONLY):
                raise RoomError("這個只能在本機做", 403)
            if path.startswith(SELF_ONLY) and actor != USER and args.get("session", actor) != actor:
                raise RoomError("只能用你自己的身分", 403)
            args["as"] = actor
            return actor

        def _route(self, method, path, a, actor):
            if method == "GET" and path in ("/", "/index.html"):
                if INDEX_HTML.exists():
                    return INDEX_HTML.read_bytes(), "text/html; charset=utf-8"
                return b"agent-rooms core", "text/plain; charset=utf-8"
            if method == "GET" and path == "/api/overview":
                return core.overview()
            if method == "POST" and path == "/api/register":
                return core.register(a["session"], a.get("kind", ""), a.get("name", ""), a.get("spawned_by"))
            if method == "POST" and path == "/api/rename":
                return core.rename(actor, a["session"], a["name"])
            if method == "POST" and path == "/api/move":
                return core.move(actor, a["session"], a.get("room_id") or None)
            if method == "GET" and path == "/api/moves":
                return core.moves(actor, a["session"])
            if method == "POST" and path == "/api/role":
                return core.set_role(actor, a["session"], a.get("role", ""))
            if method == "POST" and path == "/api/off":
                return core.set_off(actor, a["session"], a.get("on") in (True, "1", "true", 1), a.get("reason", ""))
            if method == "POST" and path == "/api/desk":
                return core.desk(actor, a["session"])
            if method == "POST" and path == "/api/keys":
                if a.get("revoke") in (True, "1", "true", 1):
                    return core.revoke_keys(actor, a["identity"])
                return core.issue_key(actor, a["identity"], a.get("label", ""))
            if method == "POST" and path == "/api/global":
                return core.set_global(actor, a["session"], a.get("kind", ""), a.get("alias", ""))
            if method == "POST" and path == "/api/user_here":
                return core.user_here(a["session"])
            if method == "POST" and path == "/api/dnd":
                return core.set_dnd(actor, a["session"], a.get("on") in (True, "1", "true", 1))
            if method == "GET" and path == "/api/listen":
                flag = lambda k, d: str(a.get(k, d)).lower() in ("1", "true")
                return core.listen(a["session"], float(a.get("wait", 50)), int(a.get("limit", 200)),
                                   triggers_only=flag("triggers_only", "0"), consume=flag("consume", "1"),
                                   heartbeat=flag("heartbeat", "1"))
            if method == "POST" and path == "/api/ack":
                return core.ack(a["session"], a["room_id"], a["seq"])
            if method == "POST" and path == "/api/touch":
                return core.touch(a["session"])
            if method == "POST" and path == "/api/delivery":
                return core.record_delivery(a["session"], a["action"], a.get("detail", ""),
                                            a.get("light") in (True, "1", "true", 1), a.get("notify") or ())
            if method == "GET" and path == "/api/deliveries":
                return core.deliveries(int(a.get("limit", 200)))
            if path == "/api/rooms":
                if method == "POST":
                    return core.create_room(actor, a.get("name", ""), a.get("owner") or None)
                return core.list_rooms(actor)
            m = re.fullmatch(r"/api/rooms/(R\d+)(?:/(\w+))?", path)
            if m:
                room_id, sub = m.groups()
                if sub is None and method == "GET":
                    return core.room(actor, room_id)
                if sub == "pos" and method == "POST":
                    return core.set_room_pos(actor, room_id, a["x"], a["y"])
                if sub == "owner" and method == "POST":
                    return core.set_owner(actor, room_id, a["session"])
                if sub == "delete" and method == "POST":
                    return core.delete_room(actor, room_id)
                if sub == "resume" and method == "POST":
                    return core.resume(actor, room_id)
                if sub == "name" and method == "POST":
                    return core.rename_room(actor, room_id, a.get("name", ""))
                if sub == "history":
                    return core.history(actor, room_id, int(a.get("since", 0)), int(a.get("limit", 200)))
                if sub == "whiteboard":
                    if method == "PUT" or method == "POST":
                        return core.set_whiteboard(actor, room_id, a.get("text", ""))
                    return core.whiteboard(actor, room_id)
                if sub == "post" and method == "POST":
                    text = a.get("text", "")
                    return core.post(actor, room_id, text, judged=core.judge_wake(actor, room_id, text))
                if sub == "threads":
                    return core.threads(actor, room_id, open_only=a.get("all") not in ("1", "true", True))
                if sub == "tasks":
                    if method == "POST":
                        return core.create_task(actor, room_id, a.get("objective", ""), a.get("criteria", ""),
                                                a.get("verifier") or None)
                    return core.tasks(actor, room_id)
            m = re.fullmatch(r"/api/threads/(L\d+)/close", path)
            if m and method == "POST":
                return core.close_thread(actor, m.group(1), a.get("note", ""))
            m = re.fullmatch(r"/api/tasks/(T\d+)/(\w+)", path)
            if m and method == "POST" and m.group(2) in TASK_ACTIONS:
                return TASK_ACTIONS[m.group(2)](core, actor, m.group(1), a)
            raise RoomError(f"沒有這個入口：{method} {path}", 404)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

    return Handler


def serve(data_dir, host="127.0.0.1", port=8775, remote_port=0):
    core = Core(data_dir)
    if remote_port:
        import threading
        remote = ThreadingHTTPServer((host, remote_port), make_handler(core, remote=True))
        remote.daemon_threads = True
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        print(f"agent-rooms remote entry (key required) on http://{host}:{remote_port}", flush=True)
    server = ThreadingHTTPServer((host, port), make_handler(core))
    server.daemon_threads = True
    print(f"agent-rooms core on http://{host}:{port}  data={data_dir}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve(os.environ.get("ROOMS_DATA", str(Path(__file__).resolve().parent.parent / "data")),
          os.environ.get("ROOMS_HOST", "127.0.0.1"), int(os.environ.get("ROOMS_PORT", "8775")),
          int(os.environ.get("ROOMS_REMOTE_PORT", "0") or 0))
