"""room：agent 與使用者用的命令列。跟 acc 一樣，agent 用 --session 帶自己的 session id。

  room join R1 --session <id>          進房（同時登記這個點）
  room say "@codex-1 看白板" --session <id>
  room listen --session <id>           一直等這間房的新訊息，一則一行（給 Claude 的 Monitor 用）
  room task submit T1 --note "..." --session <id>
  room ... --user                       以使用者的身分（也可以寫 --<使用者的 id>）
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from rooms.identity import USER, USER_NAME

USER_FLAG = [f"--{USER}"] if USER != "user" else []   # 例如 --alex：使用者的 id 也能當旗標

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
    if actor == USER:
        sys.exit("使用者要指定房間：--room R1")
    # 維修看得到每一間，但不一定在哪一間裡：只認自己是成員的那間
    mine = [r for r in rooms if any(m["session"] == actor for m in r["members"])]
    if not mine:
        sys.exit("你現在不在任何房間：先 room join <房間>，或用 --room R1 指定")
    return mine[0]["room_id"]


def agent_of(name_or_session):
    """名字或 session → session（全部登記過的點裡找）。"""
    for a in call("GET", "/api/overview")["agents"]:
        if name_or_session in (a["name"], a["session"]):
            return a["session"]
    return name_or_session


def line_for(event, room_name):
    """room_name 可以是一個名字，或 {房號: 名字}（維修一次收到好幾間房的事件）。"""
    if isinstance(room_name, dict):
        room_name = room_name.get(event["room_id"], event["room_id"])
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
    if kind == "delivery":
        return head + f"〔送不到 {event['from_name']}〕{event['detail']}"
    if kind == "off":
        why = f"：{event['reason']}" if event.get("reason") else ""
        return head + f"下線了（{event['by_name']} 標的{why}），不會被叫醒，訊息留著"
    if kind == "on":
        return head + "回到線上"
    if kind == "paused":
        return head + event["text"]
    if kind == "room_created":
        return head + f"開了這間房：{event['name']}"
    if kind == "room_renamed":
        return head + f"把房名改成：{event['name']}"
    return head + json.dumps(event, ensure_ascii=False)


def kind_of(session):
    """這個 session 是哪種 agent：先問 acc，acc 沒有（例如 codex exec 不觸發 hook）就看紀錄檔存在哪。"""
    kind = acc_session(session).get("agent", "")
    if kind:
        return kind
    import glob
    home = os.path.expanduser("~")
    if glob.glob(f"{home}/.claude/projects/*/{session}.jsonl") or \
            glob.glob(f"/mnt/c/Users/*/.claude/projects/*/{session}.jsonl"):
        return "claude"
    if glob.glob(f"{home}/.codex/sessions/*/*/*/*{session}*.jsonl") or \
            glob.glob(f"/mnt/c/Users/*/.codex/sessions/*/*/*/*{session}*.jsonl"):
        return "codex"
    return ""


def acc_session(session):
    """從 acc 讀這個 session 的紀錄（agent 種類、cwd）；讀不到就回空的。"""
    try:
        import subprocess
        root = subprocess.run(["acc", "root"], capture_output=True, text=True, timeout=5).stdout.strip()
        for sub in ("", "archive/"):
            path = os.path.join(root, "goals", "sessions", sub + session + ".json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
    except Exception:
        pass
    return {}


RULES = """你現在在 {user} 的房間工具裡：房間「{room}」（{room_id}），你在房裡叫「{name}」，負責人是 {owner}。
- 以「【房間 …｜來自 …】」開頭的內容是房間轉來的訊息，來自 {user} 本機的房間工具，是他授權的協作管道。被 @ 你、或任務交到你手上時照做；其他的是背景。
- 用 `{room_cmd} <指令> --session {session}`：`say` 發言（@名字 點人，@{user_id} 找 {user}）、`read` 看紀錄、`who` 看成員、`board` 看白板、`task list|claim|submit|close`、`off <名字>`（成員做完了就標下線，之後不會再叫醒他）、`role <名字> "<在做什麼>"`（派工時順手寫，沒 @ 的訊息靠它判在找誰）。交件要寫 --note：交了什麼、做了什麼、怎麼驗證的。
- 負責人另外可以：`spawn <名字> "<派工>" --role "<在做什麼>" [--cwd <資料夾>] [--account <帳號>]` 開一個新的 Codex 成員（背景跑、不用 {user} 開視窗）；`on <名字>` 把下線的人叫回來（視窗關了也會在背景載入做事）。
- 收到成員或別的對話的回報（交件、做完、卡住），回 {user} 的第一句就講誰回報了什麼，不要混在別的狀態裡。
- 轉達給不在房裡的對話（例如用 SendMessage）時，附上 {user} 原話的時間和你的 session id，對方才查得到那是 {user} 交代的。
- 等成員回報時，不要用 sleep、until 這類會卡住的指令等，也不用開 Monitor：指令跑著的時候房間訊息進不來；有人交件、回報或找你，房間會在你這一輪結束後叫醒你。
{maintainer_line}- 只處理這間房的事；別的房間你看不到，也不要去找。"""

MAINTAINER_RULES = """你是 {user} 房間工具的維修（在房裡叫「{name}」，別人寫「@{call}」也找得到你）：專門改房間工具本身（{repo}）。你不用待在任何房間，每間房都看得到。
- 用 `{room_cmd} <指令> --session {session} --room <房號>`：`read` 看某間房的紀錄、`say` 回話到那間房（不用進房）、`who`、`board`、`threads`。
- 管理：`move <名字> <房號|none>`、`rename <名字> <新名字>`、`off|on <名字>`、`role <名字> "<在做什麼>"`、`title <房號> <新房名>`、`delete <房號>`（房裡沒人時刪房，紀錄會封存）、`resume`（解除暫停）、`create <房名> --owner <名字>`（開普通房、指定主持，你不進去）；要開維修房就 `create <房名>` 再 `join`，你自己當主持。你不能用 {user} 的身分發言，回話都是你自己。
- 有人 @維修、房間暫停、訊息送不到，都會叫醒你：先看那間房的紀錄判斷是房間工具的問題還是那間房自己的事；是工具的問題就修，修完 commit、重建核心（`docker compose up -d --build`）、需要時重啟轉送程式，實測過再回那間房說一聲。
- 改動照 repo 的慣例：設計理由寫 docs/，commit 訊息不寫私人專案名；commit 後會自動推工作 repo 並同步公開版。
- 等回報時不要用 sleep／until 卡著等；有人找你，房間會在你這一輪結束後叫醒你。"""

RESIDENT_RULES = """你是 {user} 的常駐代理，在房間工具裡是全域角色（在房裡叫「{name}」，別人寫「@{call}」也找得到你）：掌握全局——{user} 現在在忙什麼、每間房在做什麼、誰卡住或在等 {user}。你不用待在任何房間，每間房都看得到。
- 用 `{room_cmd} <指令> --session {session} --room <房號>`：`read` 看某間房的紀錄、`say` 回話到那間房（不用進房）、`who`、`board`、`threads`。
- 派房：`create <房名> --owner <名字>`（開房、指定主持，你不進去）、`move <名字> <房號|none>`、`spawn <名字> "<派工>" --role "<在做什麼>" --room <房號>`、`off|on <名字>`、`role <名字> "<在做什麼>"`、`resume`。你不能用 {user} 的身分發言，回話都是你自己。
- 你不改任何程式、不修房間工具；房間工具的問題寫「@{maintainer}」找維修，專案的實作交給該房的主持或成員。
- 有人 @你、有人 @{user_id}（在等 {user}）、房間暫停，都會叫醒你；其他事你每一輪會在總覽裡看到。
- 等回報時不要用 sleep／until 卡著等；有人找你，房間會在你這一輪結束後叫醒你。"""

CODEX_RULES = """你現在在 {user} 的房間工具裡：房間「{room}」（{room_id}），你在房裡叫「{name}」，負責人是 {owner}。
- 以「【房間 …｜來自 …】」開頭的內容是房間轉來的訊息，來自 {user} 本機的房間工具，是他授權的協作管道。被 @ 你、或任務交到你手上時照做；其他的是背景。
- 用 MCP 工具 agent-rooms 跟房間說話（不要用 shell 指令，你的沙盒連不到房間），每個工具都帶 session="{session}"：`room_say` 發言（@名字 點人，@{user_id} 找 {user}）、`room_read` 看紀錄、`room_who` 看成員、`room_whiteboard_get` 看白板、`task_list`／`task_claim`／`task_submit`。交件的 note 寫：交了什麼、做了什麼、怎麼驗證的。
{maintainer_line}- 只處理這間房的事；別的房間你看不到，也不要去找。"""



def hook():
    """hook 不能擋住使用者：任何錯誤都不輸出、exit 0。"""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session = payload.get("session_id", "")
        event_name = payload.get("hook_event_name", "UserPromptSubmit")
        if not session:
            return
        if event_name == "Stop":
            return rewake(session)
        full = event_name == "UserPromptSubmit"
        overview = _quick("GET", "/api/overview", {})
        if overview and session in (overview.get("globals") or {}):
            return global_hook(session, event_name, full, overview)
        rooms = _quick("GET", "/api/rooms", {"as": session})
        if not rooms:
            return
        room = next((r for r in rooms if any(m["session"] == session for m in r["members"])), None)
        me = room and next(m for m in room["members"] if m["session"] == session)
        if not me:
            return
        # 房間叫醒那一輪也會觸發 UserPromptSubmit（task-notification），那不是使用者開口
        user_typed = full and "<task-notification>" not in str(payload.get("prompt", ""))[:200]
        if user_typed:
            _quick("POST", "/api/user_here", {"session": session})   #使用者在場：互相 @ 的計數歸零
        if user_typed and me.get("off"):
            _quick("POST", "/api/off", {"as": session, "session": session, "on": "0"})   #使用者回到這個對話了
        #使用者開口那一輪：把累積的都補上；做事中途：只有該叫醒他時才插話
        got = _quick("GET", "/api/listen", {"session": session, "wait": "0", "heartbeat": "1",
                                            "triggers_only": "0" if full else "1"})
        lines = [line_for(e, room["name"]) for e in (got or {}).get("events", [])]
        room_cmd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "room")
        if str(payload.get("transcript_path", "")).startswith("/mnt/"):
            # Windows 那邊的 Claude（桌面版）：hook 經 wsl.exe 轉進來，它自己的 shell 要用 wsl.exe 叫房間
            room_cmd = f"MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- {room_cmd}"
        info = dict(room=room["name"], room_id=room["room_id"], name=me["name"], owner=room["owner_name"],
                    session=session, room_cmd=room_cmd, maintainer_line=maintainer_line(overview))
        parts = []
        if full:
            parts.append((CODEX_RULES if me["kind"] == "codex" else RULES).format(**info, user=USER_NAME, user_id=USER))
        if lines:
            parts.append("房間的新訊息：\n" + "\n".join(lines))
        if not parts or (not full and not lines):
            return
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                                 "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))
    except Exception:
        return


def maintainer_line(overview):
    """一般規則裡「工具壞了找誰」那一句；沒有維修就不寫。"""
    for sid, g in ((overview or {}).get("globals") or {}).items():
        if g["kind"] == "maintainer":
            return f"- 房間工具本身壞了或要改（叫不醒、送不到、要新功能），寫「@{call_of(sid, g, overview)}」找維修，不要自己改房間工具的程式。\n"
    return ""


def call_of(session, g, overview):
    """房裡 @ 這個全域角色的寫法：有叫法用叫法，沒有就用它的名字。"""
    if g.get("alias"):
        return g["alias"]
    return next((a["name"] for a in overview["agents"] if a["session"] == session), session[:8])


def global_hook(session, event_name, full, overview):
    """全域角色：不用待在房裡。開口那一輪給規則＋每間房的總覽＋找它的訊息；做事中途只在有人找它時插話。"""
    got = _quick("GET", "/api/listen", {"session": session, "wait": "0", "heartbeat": "1",
                                        "triggers_only": "0" if full else "1"})
    names = {r["room_id"]: r["name"] for r in overview["rooms"]}
    lines = [line_for(e, names) for e in (got or {}).get("events", [])]
    if not full and not lines:
        return
    parts = []
    if full:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        me = next((a for a in overview["agents"] if a["session"] == session), {"name": session[:8]})
        g = overview["globals"][session]
        rules = MAINTAINER_RULES if g["kind"] == "maintainer" else RESIDENT_RULES
        keeper = next((call_of(s, k, overview) for s, k in overview["globals"].items() if k["kind"] == "maintainer"),
                      "維修")
        parts.append(rules.format(user=USER_NAME, user_id=USER, name=me["name"], call=call_of(session, g, overview), session=session,
                                  room_cmd=os.path.join(repo, "bin", "room"), repo=repo, maintainer=keeper))
        parts.append(summary_of(overview))
    if lines:
        parts.append("找你的訊息：\n" + "\n".join(lines))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                             "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))


def summary_of(overview):
    """每間房一段：負責人、成員狀態、暫停／亮燈、最後幾則。沒人在的房間收成一行。"""
    agents = {a["session"]: a for a in overview["agents"]}
    active, idle = [], []
    for r in overview["rooms"]:
        if not r["members"] and not r.get("lit"):
            idle.append(f"{r['room_id']} {r['name']}")
            continue
        flags = [f for f, on in (("暫停中", r.get("paused")), (f"亮燈 {r.get('lit')}", r.get("lit"))) if on]
        people = []
        for m in r["members"]:
            state = "下線" if m.get("off") else ("勿擾" if m.get("dnd") else "")
            fail = agents.get(m["session"], {}).get("delivery") or {}
            if fail.get("action") in FAILED:
                state = (state + "，" if state else "") + f"送不到（{fail['action']}）"
            people.append(m["name"] + (f"（{state}）" if state else ""))
        lines = [f"■ {r['room_id']} {r['name']}｜負責人 {r['owner_name']}"
                 + (f"｜{'、'.join(flags)}" if flags else "") + f"｜成員：{'、'.join(people) or '（沒人）'}"]
        for e in r.get("recent", []):
            lines.append(f"  #{e['seq']} {e['at'][5:16]} {e['from']}：{e['text'][:100]}")
        active.append("\n".join(lines))
    text = "各房間現況：\n" + "\n".join(active)
    if idle:
        text += "\n（沒人在的房間：" + "、".join(idle) + "）"
    return text


FAILED = ("uncertain", "failed_after", "unavailable", "detached", "not_found", "desktop_closed")


def rewake(session):
    """Stop hook（asyncRewake）：這一輪做完就在背景等房間，有人找他時 exit 2 把他叫醒。

    閒著的 Claude 沒有別的入口叫得醒；Monitor 要它自己先開，所以改成每輪結束都自動接上。
    同一個對話只留最新的一個等待者（新的一輪結束會換掉舊的）。不在房裡就慢慢等，進房後自動接上。
    """
    import tempfile
    mark = os.path.join(tempfile.gettempdir(), f"agent-rooms-wait-{session}")
    me = str(os.getpid())
    with open(mark, "w") as fh:
        fh.write(me)
    def mine():
        try:
            with open(mark) as fh:
                return fh.read().strip() == me
        except OSError:
            return False

    while mine():
        try:
            # 先不推進游標：等確定自己還是最新的等待者才 ack，舊的等待者不會把訊息吃掉
            got = call("GET", "/api/listen", session=session, wait=50, triggers_only="1", consume="0")
        except (CoreDown, SystemExit):
            time.sleep(20)                  # 核心沒開，或這個對話還沒進過房間
            continue
        if not got["events"]:
            if not got["room_id"]:
                time.sleep(10)
            continue
        if not mine():
            return                          # 已經有新的等待者接手
        last = {}
        for e in got["events"]:
            last[e["room_id"]] = max(last.get(e["room_id"], 0), e["seq"])
        for room_id, seq in last.items():
            try:
                call("POST", "/api/ack", session=session, room_id=room_id, seq=seq)
            except (CoreDown, SystemExit):
                pass
        names = {r["room_id"]: r["name"] for r in (_quick("GET", "/api/rooms", {"as": session}) or [])}
        lines = [line_for(e, names) for e in got["events"]]
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()
        sys.exit(2)


def _quick(method, path, params):
    """hook 用：兩秒內沒回就算了，不能拖慢使用者。"""
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return payload.get("result") if payload.get("ok") else None


def show(result):
    print(json.dumps(result, ensure_ascii=False, indent=1))


def spawn(actor, room_id, name, role, text, cwd, account=None):
    """開新的 Codex 對話 → 登記成房間開的成員 → 進房 → 寫在做什麼 → @ 他發第一則訊息。

    第一則由轉送程式送進去（帶房間規則），跟其他成員收訊息走同一條路。
    """
    room = call("GET", f"/api/rooms/{room_id}", actor)
    if actor != USER and room["owner"] != actor and actor not in call("GET", "/api/overview")["globals"]:
        sys.exit("只有使用者、全域角色或房間負責人能開新成員")
    if not os.path.isdir(cwd):
        sys.exit(f"沒有這個資料夾：{cwd}")
    from rooms import dispatch
    try:
        thread_id, home = dispatch.spawn(os.path.abspath(cwd), account)
    except RuntimeError as error:
        sys.exit(f"錯誤：{error}")
    call("POST", "/api/register", session=thread_id, kind="codex", name=name, spawned_by=actor)
    call("POST", "/api/move", thread_id, session=thread_id, room_id=room_id)
    call("POST", "/api/role", actor, session=thread_id, role=role)
    posted = call("POST", f"/api/rooms/{room_id}/post", actor, text=f"@{name} {text}")
    show({"spawned": thread_id, "name": name, "account": dispatch.account_name(home), "cwd": os.path.abspath(cwd),
          "attach": dispatch.resume_hint(thread_id, home), "post": posted})


def main(argv=None):
    p = argparse.ArgumentParser(prog="room", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", help="自己的 session id（agent 用）")
    p.add_argument("--user", *USER_FLAG, dest="as_user", action="store_true", help=f"以使用者（{USER_NAME}）的身分")
    p.add_argument("--room", help="指定房間；agent 不填就是自己所在的那間")
    # 同一組參數寫在子指令後面也收得到（agent 習慣寫在最後）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default=argparse.SUPPRESS)
    common.add_argument("--user", *USER_FLAG, dest="as_user", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--room", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)

    s = sub.add_parser("register"); s.add_argument("--kind", default=""); s.add_argument("--name", default="")
    s = sub.add_parser("rename"); s.add_argument("target"); s.add_argument("name")
    s = sub.add_parser("create"); s.add_argument("name")
    s.add_argument("--owner", default=None, help="使用者／維修：指定負責人（名字或 session），自己不進去")
    s = sub.add_parser("global", help="使用者指定全域角色：maintainer（改房間工具）、resident（常駐代理，不改程式）")
    s.add_argument("target"); s.add_argument("kind", choices=["maintainer", "resident", "none"])
    s.add_argument("--alias", default="", help="房裡 @ 它的另一個叫法，例如「維修」")
    s = sub.add_parser("delete", help="刪房間（使用者或全域角色；房裡要先沒人，紀錄會封存）"); s.add_argument("room_id")
    sub.add_parser("resume", help="解除暫停（使用者、維修或負責人）")
    s = sub.add_parser("title", help="改房名（負責人或使用者）"); s.add_argument("room_id"); s.add_argument("name")
    s = sub.add_parser("join"); s.add_argument("room_id"); s.add_argument("--kind", default=""); s.add_argument("--name", default="")
    sub.add_parser("leave")
    s = sub.add_parser("move", help="使用者把某個點移進房間（room 填 none 就是移出）"); s.add_argument("target"); s.add_argument("room_id")
    sub.add_parser("moves")
    sub.add_parser("rooms")
    sub.add_parser("who")
    s = sub.add_parser("read"); s.add_argument("--since", type=int, default=0); s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("say"); s.add_argument("text")
    s = sub.add_parser("board"); s.add_argument("--set", metavar="FILE", help="用檔案內容取代白板（- 是 stdin）")
    s = sub.add_parser("threads"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("close-thread"); s.add_argument("thread_id"); s.add_argument("--note", default="")
    s = sub.add_parser("off", help="把成員標成下線（做完了、視窗關了）：不叫醒，訊息留著")
    s.add_argument("target", nargs="?", help="名字或 session；不填就是自己"); s.add_argument("--reason", default="")
    s = sub.add_parser("on", help="把成員標回線上，留著的訊息會補送"); s.add_argument("target", nargs="?")
    s = sub.add_parser("spawn", help="負責人開一個新的 Codex 成員（背景跑、沒有視窗），進房後收到第一則訊息")
    s.add_argument("name"); s.add_argument("text", help="給他的第一則訊息（派工）")
    s.add_argument("--role", required=True, help="一句話：他在做什麼")
    s.add_argument("--cwd", default=None, help="工作資料夾，預設是現在的資料夾")
    s.add_argument("--account", default=None, help="指定 Codex 帳號（ROOMS_CODEX_HOMES 裡的名字）；預設照 ROOMS_SPAWN_ACCOUNTS 挑還有額度的")
    s = sub.add_parser("role", help="寫成員在做什麼（一句話）：沒 @ 的訊息由判官照這個判在找誰")
    s.add_argument("target"); s.add_argument("text")
    s = sub.add_parser("dnd"); s.add_argument("state", choices=["on", "off"]); s.add_argument("--target")
    s = sub.add_parser("listen"); s.add_argument("--once", action="store_true"); s.add_argument("--wait", type=float, default=50)
    s.add_argument("--every", action="store_true", help="每個事件都回（預設只在被 @、任務跟你有關時才回，背景一起帶上）")
    s = sub.add_parser("hook", help="給 Claude／Codex 的 hook 用：stdin 讀 hook 的 JSON，有房間訊息就注入")
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
    if a.as_user and a.session:
        sys.exit("--session 和 --user 只能選一個")
    actor = USER if a.as_user else a.session
    if a.cmd == "hook":
        return hook()
    if not actor and a.cmd not in ("overview",):
        sys.exit("要帶 --session <自己的 session id>，或 --user")

    if a.cmd == "register":
        return show(call("POST", "/api/register", session=actor, kind=a.kind, name=a.name))
    if a.cmd == "rename":
        return show(call("POST", "/api/rename", actor, session=agent_of(a.target), name=a.name))
    if a.cmd == "create":
        return show(call("POST", "/api/rooms", actor, name=a.name, owner=agent_of(a.owner) if a.owner else None))
    if a.cmd == "global":
        return show(call("POST", "/api/global", actor, session=agent_of(a.target),
                         kind="" if a.kind == "none" else a.kind, alias=a.alias))
    if a.cmd == "delete":
        return show(call("POST", f"/api/rooms/{a.room_id}/delete", actor))
    if a.cmd == "title":
        return show(call("POST", f"/api/rooms/{a.room_id}/name", actor, name=a.name))
    if a.cmd == "join":
        call("POST", "/api/register", session=actor, kind=a.kind or kind_of(actor), name=a.name)
        return show(call("POST", "/api/move", actor, session=actor, room_id=a.room_id))
    if a.cmd == "leave":
        return show(call("POST", "/api/move", actor, session=actor, room_id=None))
    if a.cmd == "move":
        target = agent_of(a.target)
        call("POST", "/api/register", session=target, kind=kind_of(target))
        return show(call("POST", "/api/move", actor, session=target, room_id=None if a.room_id == "none" else a.room_id))
    if a.cmd == "moves":
        return show(call("GET", "/api/moves", actor, session=actor))
    if a.cmd == "rooms":
        return show(call("GET", "/api/rooms", actor))
    if a.cmd == "overview":
        return show(call("GET", "/api/overview"))
    if a.cmd == "role":
        return show(call("POST", "/api/role", actor, session=agent_of(a.target), role=a.text))
    if a.cmd == "spawn":
        return spawn(actor, current_room(actor, a.room), a.name, a.role, a.text, a.cwd or os.getcwd(), a.account)
    if a.cmd in ("off", "on"):
        target = agent_of(a.target) if a.target else actor
        return show(call("POST", "/api/off", actor, session=target, on=a.cmd == "off", reason=getattr(a, "reason", "")))
    if a.cmd == "dnd":
        return show(call("POST", "/api/dnd", actor, session=a.target or actor, on=a.state == "on"))
    if a.cmd == "listen":
        if actor == USER:
            sys.exit("listen 是給 agent 用的")
        down = False
        while True:
            try:
                got = call("GET", "/api/listen", session=actor, wait=a.wait,
                       triggers_only=None if a.every else "1")
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
    if a.cmd == "resume":
        return show(call("POST", f"/api/rooms/{room}/resume", actor))
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
