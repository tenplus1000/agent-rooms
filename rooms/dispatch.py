"""轉送程式：替房裡的 Codex 監聽，該叫醒時用轉接器送進它的對話。跑在本機，不進 container。

Claude 不經過這裡：它自己用 Monitor 跑 `room listen`，做事中途由 PostToolUse hook 補送。

每個 Codex 一條執行緒：
  1. 對核心長輪詢（只等「該叫醒他」的事件，不推進游標、不算心跳）
  2. 使用者 三分鐘內在那個對話講過話 → 延後（照助理 wakeup_runner），不算次數
  3. 找出這個對話在哪：桌面版、或 WSL 的哪個 Codex 帳號
  4. 問狀態：正在跑 → turn/steer；閒著 → turn/start；其他（額度用完、桌面版沒開）→ 記錄、亮燈、過一陣子再試
  5. 送成功才 ack；送出後結果不確定 → 也 ack（不自動重送），記錄並亮燈，請 使用者 查紀錄

每一次叫醒、插話、延後、擋下都寫進核心的 deliveries.jsonl。
"""

import datetime
import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("ROOMS_URL", "http://127.0.0.1:8775")
ACC_SESSIONS = os.environ.get("ACC_SESSIONS", "/mnt/c/Users/<you>/.agent-context/goals/sessions")
DESKTOP = os.path.join(ROOT, "adapters", "codex_desktop", "codex-desktop")
CLI = os.path.join(ROOT, "adapters", "codex_cli", "codex-cli")
# 桌面版的紀錄在 Windows；WSL 的兩個 Codex 帳號共用同一個 sessions（account2 的是捷徑），
# 所以帳號不能從紀錄放哪判斷，要直接問兩個 daemon。
DESKTOP_SESSIONS = "/mnt/c/Users/<you>/.codex/sessions"
WSL_SESSIONS = os.path.expanduser("~/.codex/sessions")
ACCOUNTS = [os.path.expanduser("~/.codex"), os.path.expanduser("~/.codex-work")]
DASHBOARD = os.path.expanduser("~/quota-helper")      # 借它的 codexquota 查額度（唯讀、不叫模型）

USER_IDLE = 180          # 使用者 講完話三分鐘內不插進那個對話
OWN_ECHO = 20             # 我們自己送進去的那一輪也會觸發 hook、更新 last_seen，這段時間內的不算 使用者
AFTER_CHECK = 8           # 送出後幾秒回頭確認那一輪有沒有跑起來
CAP_PER_HOUR = 30         # 每個 agent 每小時最多被房間叫醒幾次
BACKOFF = {"unavailable": 600, "desktop_closed": 300, "race": 3, "error": 60, "detached": 60}
DETACHED = "already has an active writer"   # 視窗自己握著對話、沒接背景服務：daemon 確定沒收下


def call(method, path, **params):
    params = {k: v for k, v in params.items() if v is not None}
    url = BASE + path
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=70) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error"))
    return payload["result"]


def log(*parts):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *parts, flush=True)


def _found(root, thread_id):
    return bool(glob.glob(os.path.join(root, "*", "*", "*", f"*{thread_id}*.jsonl")))


def _sock(home):
    return os.path.join(home, "app-server-control", "app-server-control.sock")


def _has_quota(home):
    """這個帳號現在還能跑嗎？查不到就當可以（讓 daemon 自己回錯）。"""
    try:
        if DASHBOARD not in sys.path:
            sys.path.append(DASHBOARD)
        import codexquota
        info = codexquota.read(home)
    except Exception:
        return True
    if info.get("error"):
        return True
    return all((w or {}).get("used", 0) < 100 for w in (info.get("five_hours"), info.get("week")))


def locate(thread_id):
    """這個 Codex 對話要怎麼送：回 (種類, socket 或 None, 說明)。找不到回 None。

    桌面版 → 桌面轉接器。WSL 的對話 → 先找哪個帳號的 daemon 已經掛著它（有視窗開著就是那個），
    都沒掛就挑還有額度的帳號；兩個帳號都沒額度回 ("cli", None, 原因)。
    """
    if _found(DESKTOP_SESSIONS, thread_id):
        return "desktop", None, "桌面版"
    if not _found(WSL_SESSIONS, thread_id):
        return None
    for home in ACCOUNTS:
        state, error = adapter("cli", _sock(home), {"method": "thread/state", "params": {"threadId": thread_id}})
        if not error and (state.get("threadRuntimeStatus") or {}).get("type") in ("idle", "active"):
            return "cli", _sock(home), f"{os.path.basename(home)}（對話掛在這個帳號）"
    for home in ACCOUNTS:
        if _has_quota(home):
            return "cli", _sock(home), f"{os.path.basename(home)}（還有額度）"
    return "cli", None, "兩個 Codex 帳號的額度都用完了"


def adapter(kind, sock, request):
    env = dict(os.environ)
    if sock:
        env["CODEX_CLI_SOCKET"] = sock
    exe = DESKTOP if kind == "desktop" else CLI
    proc = subprocess.run([exe, "rpc"], input=json.dumps(request, ensure_ascii=False), text=True,
                          capture_output=True, env=env, timeout=90)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return None, f"轉接器沒有回 JSON（exit {proc.returncode}）：{proc.stderr.strip()[:200]}"
    if "error" in out:
        return None, out["error"].get("message", "")
    return out.get("result"), None


def user_active(session, last_sent):
    """使用者 剛在這個對話講過話嗎？看 acc 的 last_seen（每次 UserPromptSubmit 都會更新）。"""
    try:
        with open(os.path.join(ACC_SESSIONS, session + ".json"), encoding="utf-8") as fh:
            seen = datetime.datetime.fromisoformat(json.load(fh)["last_seen"]).timestamp()
    except Exception:
        return 0
    if last_sent and abs(seen - last_sent) < OWN_ECHO:
        return 0                                  # 那是我們送進去的那一輪
    left = USER_IDLE - (time.time() - seen)
    return max(0, left)


def render(room, session, events):
    """送進 Codex 的內容：房間規則（它沒有房間 hook）＋這一批訊息。"""
    from rooms.cli import CODEX_RULES as RULES, line_for
    me = next((m for m in room["members"] if m["session"] == session), {"name": session[:8]})
    rules = RULES.format(room=room["name"], room_id=room["room_id"], name=me["name"], owner=room["owner_name"],
                         session=session, room_cmd=os.path.join(ROOT, "bin", "room"))
    lines = [line_for(e, room["name"]) for e in events]
    return rules + "\n\n房間的新訊息：\n" + "\n".join(lines)


class Worker(threading.Thread):
    def __init__(self, session):
        super().__init__(daemon=True)
        self.session = session
        self.sent = []            # 最近一小時送出的時間
        self.last_sent = 0
        self.stop = False
        self.reported = None      # 這一批送不到已經通知過了（重試時不再洗版）

    def note(self, action, detail, light=False, notify=()):
        log(self.session[:8], action, detail)
        try:
            call("POST", "/api/delivery", session=self.session, action=action, detail=detail, light=light,
                 notify=list(notify))
        except Exception as error:
            log("記錄失敗", error)

    def fail(self, action, detail, events):
        """送不到：亮燈，並在房裡告訴找他的人。同一批只通知一次，之後重試只記紀錄。"""
        key = (action, events[-1]["seq"])
        if self.reported == key:
            log(self.session[:8], action, "（已通知過）", detail)
            return
        self.reported = key
        senders = []
        for e in events:
            if self.session in e.get("wake", ()) and e["from"] not in senders:
                senders.append(e["from"])
        self.note(action, detail, light=True, notify=senders)

    def run(self):
        held = None
        while not self.stop:
            try:
                got = call("GET", "/api/listen", session=self.session, wait=30, triggers_only=1,
                           consume=0, heartbeat=0)
                if not got["events"]:
                    continue
                room_id, events = got["room_id"], got["events"]
                wait = user_active(self.session, self.last_sent)
                if wait:
                    if held != events[-1]["seq"]:
                        self.note("delayed", f"使用者 剛在這個對話講過話，{int(wait)} 秒後再送")
                        held = events[-1]["seq"]
                    time.sleep(min(wait, 30))
                    continue
                self.sent = [t for t in self.sent if time.time() - t < 3600]
                if len(self.sent) >= CAP_PER_HOUR:
                    if held != ("cap", events[-1]["seq"]):
                        self.note("capped", f"一小時內已叫醒 {CAP_PER_HOUR} 次，先停；訊息留著", light=True)
                        held = ("cap", events[-1]["seq"])
                    time.sleep(60)
                    continue
                if not self.stop:               # 剛離開房間就不送了
                    self.deliver(room_id, events)
            except Exception as error:
                log(self.session[:8], "錯誤", error)
                time.sleep(BACKOFF["error"])

    def deliver(self, room_id, events):
        where = locate(self.session)
        if where is None:
            self.fail("not_found", "找不到這個 Codex 對話的紀錄（不是 Codex，或還沒開始過）", events)
            time.sleep(BACKOFF["unavailable"])
            return
        kind, sock, why = where
        if kind == "cli" and sock is None:
            self.fail("unavailable", why + "；訊息留著，之後會再試", events)
            time.sleep(BACKOFF["unavailable"])
            return
        state, error = adapter(kind, sock, {"method": "thread/state", "params": {"threadId": self.session}})
        if error:
            reason = "desktop_closed" if kind == "desktop" else "unavailable"
            self.fail(reason, f"問不到對話狀態：{error}；訊息留著，之後會再試", events)
            time.sleep(BACKOFF[reason])
            return
        call("POST", "/api/touch", session=self.session)
        status = (state.get("threadRuntimeStatus") or {}).get("type")
        if status == "active":
            method = "turn/steer"
        elif status in ("idle", "notLoaded"):
            method = "turn/start"
        else:
            self.fail("unavailable", f"對話狀態是 {status}（可能是額度用完），訊息留著，之後會再試", events)
            time.sleep(BACKOFF["unavailable"])
            return
        room = call("GET", f"/api/rooms/{room_id}", **{"as": self.session})
        text = render(room, self.session, events)
        result, error = adapter(kind, sock, {"method": method, "params": {
            "threadId": self.session, "input": [{"type": "text", "text": text}]}})
        if error and "No message sent" in error:
            log(self.session[:8], "狀態剛好變了，重試", error)
            time.sleep(BACKOFF["race"])
            return
        if error and DETACHED in error:
            self.fail("detached", "那個 Codex 視窗沒接上房間（帶了 -c 參數開的視窗會自己獨立跑），訊息留著；"
                      f"使用者 關掉那個視窗、用 `codex-work resume {self.session}`（或 `codex resume`）重開後會自動補送", events)
            time.sleep(BACKOFF["detached"])
            return
        # 送出去了，或結果不確定：都推進游標，不自動重送
        call("POST", "/api/ack", session=self.session, room_id=room_id, seq=events[-1]["seq"])
        self.last_sent = time.time()
        self.sent.append(self.last_sent)
        if error:
            self.fail("uncertain", f"{method} 送出後結果不確定（不會自動重送），請看那個對話有沒有收到：{error}", events)
            return
        self.reported = None
        self.note(method.split("/")[1], f"{why} 送了 {len(events)} 則（到 #{events[-1]['seq']}）")
        # 送進去不等於跑得起來（例如額度用完會在幾秒內失敗）：回頭看一眼
        time.sleep(AFTER_CHECK)
        after, error = adapter(kind, sock, {"method": "thread/state", "params": {"threadId": self.session}})
        status = None if error else (after.get("threadRuntimeStatus") or {}).get("type")
        if status == "systemError":
            self.fail("failed_after", "訊息送進去了，但那一輪沒跑起來（對話變成錯誤狀態，常見是額度用完）", events)


def main():
    workers = {}
    log("轉送程式啟動，核心", BASE)
    while True:
        try:
            agents = call("GET", "/api/overview")["agents"]
            wanted = {a["session"] for a in agents if a["kind"] == "codex" and a.get("room_id")}
            for session in wanted - workers.keys():
                workers[session] = Worker(session)
                workers[session].start()
                log(session[:8], "開始替它監聽")
            for session in list(workers.keys() - wanted):
                workers.pop(session).stop = True     # 等這輪長輪詢結束就停
                log(session[:8], "離開房間，停止監聽")
        except Exception as error:
            log("核心連不上", error)
        time.sleep(3)


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    main()
