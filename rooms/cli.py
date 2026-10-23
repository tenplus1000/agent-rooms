"""room：agent 與 使用者 用的命令列。跟 acc 一樣，agent 用 --session 帶自己的 session id。

  room join R1 --session <id>          進房（同時登記這個點）
  room say "@codex-1 看白板" --session <id>
  room listen --session <id>           一直等這間房的新訊息，一則一行（給 Claude 的 Monitor 用）
  room task submit T1 --note "..." --session <id>
  room ... --user                      以 使用者 的身分
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ROOMS_URL", "http://127.0.0.1:8775")


class CoreDown(Exception):
    """核心連不上（還沒開、重啟中、搬家中）。"""


def call(method, path, actor=None, **params):
    params = {k: v for k, v in params.items() if v is not None}
    if actor:
        params["as"] = actor
    url = BASE + path
    data = None
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params)
    else:
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=70) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
        raise CoreDown(f"連不上房間核心 {BASE}：{getattr(error, 'reason', error)}")
    if not payload.get("ok"):
        sys.exit(f"錯誤：{payload.get('error')}")
    return payload["result"]


def current_room(actor, given=None):
    if given:
        return given
    rooms = call("GET", "/api/rooms", actor)
    if actor == "user":
        sys.exit("使用者 要指定房間：--room R1")
    if not rooms:
        sys.exit("你現在不在任何房間。先 room join <房間>")
    return rooms[0]["room_id"]


def line_for(event, room_name):
    head = f"【房間 {room_name}（{event['room_id']}）｜來自 {event['from_name']}】"
    kind = event["type"]
    if kind == "post":
        return head + event["text"]
    if kind == "task":
        note = f"：{event['note']}" if event.get("note") else ""
        return head + f"任務 {event['task_id']} {event['action']}（{event['status']}）{event['objective']}{note}"
    if kind == "join":
        return head + "進了房間"
    if kind == "leave":
        return head + "離開房間"
    if kind == "whiteboard":
        return head + "改了白板（room board 看內容）"
    if kind == "room_created":
        return head + f"開了這間房：{event['name']}"
    return head + json.dumps(event, ensure_ascii=False)


def show(result):
    print(json.dumps(result, ensure_ascii=False, indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="room", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", help="自己的 session id（agent 用）")
    p.add_argument("--user", action="store_true", help="以 使用者 的身分")
    p.add_argument("--room", help="指定房間；agent 不填就是自己所在的那間")
    # 同一組參數寫在子指令後面也收得到（agent 習慣寫在最後）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default=argparse.SUPPRESS)
    common.add_argument("--user", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--room", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)

    s = sub.add_parser("register"); s.add_argument("--kind", default=""); s.add_argument("--name", default="")
    s = sub.add_parser("rename"); s.add_argument("target"); s.add_argument("name")
    s = sub.add_parser("create"); s.add_argument("name")
    s = sub.add_parser("join"); s.add_argument("room_id"); s.add_argument("--kind", default=""); s.add_argument("--name", default="")
    sub.add_parser("leave")
    s = sub.add_parser("move", help="使用者 把某個點移進房間（room 填 none 就是移出）"); s.add_argument("target"); s.add_argument("room_id")
    sub.add_parser("moves")
    sub.add_parser("rooms")
    sub.add_parser("who")
    s = sub.add_parser("read"); s.add_argument("--since", type=int, default=0); s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("say"); s.add_argument("text")
    s = sub.add_parser("board"); s.add_argument("--set", metavar="FILE", help="用檔案內容取代白板（- 是 stdin）")
    s = sub.add_parser("threads"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("close-thread"); s.add_argument("thread_id"); s.add_argument("--note", default="")
    s = sub.add_parser("dnd"); s.add_argument("state", choices=["on", "off"]); s.add_argument("--target")
    s = sub.add_parser("listen"); s.add_argument("--once", action="store_true"); s.add_argument("--wait", type=float, default=50)
    sub.add_parser("overview")

    t = sub.add_parser("task").add_subparsers(dest="action", required=True)
    _add_t = t.add_parser
    t.add_parser = lambda *x, **k: _add_t(*x, parents=[common], **k)
    s = t.add_parser("new"); s.add_argument("objective"); s.add_argument("--criteria", required=True); s.add_argument("--verifier")
    t.add_parser("list")
    s = t.add_parser("claim"); s.add_argument("task_id")
    s = t.add_parser("submit"); s.add_argument("task_id"); s.add_argument("--note", required=True)
    s = t.add_parser("close"); s.add_argument("task_id"); s.add_argument("result", choices=["achieved", "failed"]); s.add_argument("--note", required=True)
    s = t.add_parser("park"); s.add_argument("task_id"); s.add_argument("--reason", required=True)
    s = t.add_parser("revise"); s.add_argument("task_id"); s.add_argument("--objective", default=""); s.add_argument("--criteria", default=""); s.add_argument("--reason", required=True)
    s = t.add_parser("drop"); s.add_argument("task_id"); s.add_argument("--reason", required=True)
    s = t.add_parser("amend"); s.add_argument("task_id"); s.add_argument("to", choices=["achieved", "failed", "review"]); s.add_argument("--reason", required=True)

    a = p.parse_args(argv)
    if a.user and a.session:
        sys.exit("--session 和 --user 只能選一個")
    actor = "user" if a.user else a.session
    if not actor and a.cmd not in ("overview",):
        sys.exit("要帶 --session <自己的 session id>，或 --user")

    if a.cmd == "register":
        return show(call("POST", "/api/register", session=actor, kind=a.kind, name=a.name))
    if a.cmd == "rename":
        return show(call("POST", "/api/rename", actor, session=a.target, name=a.name))
    if a.cmd == "create":
        return show(call("POST", "/api/rooms", actor, name=a.name))
    if a.cmd == "join":
        call("POST", "/api/register", session=actor, kind=a.kind, name=a.name)
        return show(call("POST", "/api/move", actor, session=actor, room_id=a.room_id))
    if a.cmd == "leave":
        return show(call("POST", "/api/move", actor, session=actor, room_id=None))
    if a.cmd == "move":
        return show(call("POST", "/api/move", actor, session=a.target, room_id=None if a.room_id == "none" else a.room_id))
    if a.cmd == "moves":
        return show(call("GET", "/api/moves", actor, session=actor))
    if a.cmd == "rooms":
        return show(call("GET", "/api/rooms", actor))
    if a.cmd == "overview":
        return show(call("GET", "/api/overview"))
    if a.cmd == "dnd":
        return show(call("POST", "/api/dnd", actor, session=a.target or actor, on=a.state == "on"))
    if a.cmd == "listen":
        if actor == "user":
            sys.exit("listen 是給 agent 用的")
        down = False
        while True:
            try:
                got = call("GET", "/api/listen", session=actor, wait=a.wait)
                if down:
                    print("〔房間核心接回來了，繼續監聽〕", flush=True)
                    down = False
                if got["events"]:
                    name = call("GET", f"/api/rooms/{got['room_id']}", actor)["name"]
                    for event in got["events"]:
                        print(line_for(event, name), flush=True)
            except CoreDown as error:
                if a.once:
                    sys.exit(str(error))
                if not down:      # 斷線只講一次，接回來再講一次
                    print(f"〔房間核心斷線，持續重連中：{error}〕", flush=True)
                    down = True
                time.sleep(3)
                continue
            if a.once:
                return
            if not got["room_id"]:
                time.sleep(5)      # 不在房裡就慢慢等，進房後自動接上

    room = current_room(actor, a.room)
    if a.cmd == "who":
        return show(call("GET", f"/api/rooms/{room}", actor))
    if a.cmd == "read":
        name = call("GET", f"/api/rooms/{room}", actor)["name"]
        for event in call("GET", f"/api/rooms/{room}/history", actor, since=a.since, limit=a.limit):
            print(f"#{event['seq']} {event['at']} " + line_for(event, name))
        return
    if a.cmd == "say":
        return show(call("POST", f"/api/rooms/{room}/post", actor, text=a.text))
    if a.cmd == "board":
        if a.set:
            text = sys.stdin.read() if a.set == "-" else open(a.set, encoding="utf-8").read()
            return show(call("PUT", f"/api/rooms/{room}/whiteboard", actor, text=text))
        print(call("GET", f"/api/rooms/{room}/whiteboard", actor))
        return
    if a.cmd == "threads":
        return show(call("GET", f"/api/rooms/{room}/threads", actor, all="1" if a.all else None))
    if a.cmd == "close-thread":
        return show(call("POST", f"/api/threads/{a.thread_id}/close", actor, note=a.note))
    if a.cmd == "task":
        if a.action == "new":
            return show(call("POST", f"/api/rooms/{room}/tasks", actor, objective=a.objective,
                             criteria=a.criteria, verifier=a.verifier))
        if a.action == "list":
            return show(call("GET", f"/api/rooms/{room}/tasks", actor))
        extra = {k: getattr(a, k) for k in ("note", "result", "reason", "objective", "criteria", "to") if hasattr(a, k)}
        if a.action == "close":
            extra["result"] = a.result
        return show(call("POST", f"/api/tasks/{a.task_id}/{a.action}", actor, **extra))


if __name__ == "__main__":
    try:
        main()
    except CoreDown as error:
        sys.exit(str(error))
