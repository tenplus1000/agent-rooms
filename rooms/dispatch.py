"""轉送程式：替房裡的 Codex 監聽，該叫醒時用轉接器送進它的對話。跑在本機，不進 container。

Claude 平常不經過這裡：每一輪結束時 Stop hook 在背景等房間、有人找就叫醒它，做事中途由 PostToolUse hook 補送。
只有視窗關掉了（沒有程式開著那個對話，Stop hook 也跟著沒了）又有人找它時，這裡替它開一個新視窗
接回那個對話（ClaudeWorker），第一句以 REOPEN_MARK 開頭，房間的新訊息由 UserPromptSubmit hook 帶進去。

每個 Codex 一條執行緒：
  1. 對核心長輪詢（只等「該叫醒他」的事件，不推進游標、不算心跳）
  2. 找出這個對話在哪：桌面版、或 WSL 的哪個 Codex 帳號
  3. 問狀態：閒著 → turn/start；正在跑 → turn/steer，但那一輪是使用者剛開的（他三分鐘內在那個
     對話講過話）就等它跑完再送，不插進他的對話；視窗關了（notLoaded、也沒有程式開著它）→
     標成下線、只留訊息，不替他跑一輪、不花額度。使用者回到那個對話打字（acc 的 last_seen 更新）
     就標回線上，留著的一起補送。做完的成員由負責人用 `room off` 標下線，效果一樣。
     負責人或使用者用 `room on` 叫回來的、或 `room spawn` 開的（headless）→ 沒有視窗也在背景載入送進去；
     其他（額度用完、桌面版沒開）→ 記錄、亮燈、過一陣子再試
  5. 送成功才 ack；送出後結果不確定 → 也 ack（不自動重送），記錄並亮燈，請使用者查紀錄

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

from rooms import paths
from rooms.identity import OWNER_LABEL, REOPEN_MARK, USER, USER_NAME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("ROOMS_URL", "http://127.0.0.1:8775")
ACC_SESSIONS = str(paths.acc_sessions())
DESKTOP = os.path.join(ROOT, "adapters", "codex_desktop", "codex-desktop")
CLI = os.path.join(ROOT, "adapters", "codex_cli", "codex-cli")
# 桌面版的紀錄在 Windows。WSL 上可以有好幾個 Codex 帳號（各自的 CODEX_HOME），它們可能共用同一個
# sessions 資料夾，所以帳號不能從紀錄放哪判斷，要直接問每個帳號的 daemon。
DESKTOP_SESSIONS = str(paths.desktop_codex_sessions() or "/nonexistent")
WSL_SESSIONS = os.path.expanduser("~/.codex/sessions")
# ROOMS_CODEX_HOMES：用冒號分隔的 CODEX_HOME 清單（第一個優先）；每一項可以寫成「名字=路徑」，
# 名字給 `room spawn --account` 用，沒寫就是資料夾名稱。沒設就只有 ~/.codex
NAMES = {}
for _entry in (os.environ.get("ROOMS_CODEX_HOMES", "").strip() or "~/.codex").split(":"):
    if _entry.strip():
        _name, _, _path = _entry.strip().rpartition("=")
        _path = os.path.expanduser(_path)
        NAMES[_path] = _name or os.path.basename(_path)
ACCOUNTS = list(NAMES)
# ROOMS_SPAWN_ACCOUNTS：`room spawn` 開新對話時挑帳號的順序（用名字，冒號分隔）；沒設就照 ACCOUNTS
SPAWN_ORDER = [n for n in os.environ.get("ROOMS_SPAWN_ACCOUNTS", "").split(":") if n.strip()]
# 選用：放著 codexquota.py（read(codex_home) → 五小時／一週用量百分比）的資料夾，用來挑還有額度的帳號
QUOTA_HELPER = os.environ.get("ROOMS_QUOTA_HELPER", "").strip()

USER_IDLE = 180          #使用者講完話三分鐘內不插進那個對話
OWN_ECHO = 20             # 我們自己送進去的那一輪也會觸發 hook、更新 last_seen，這段時間內的不算使用者
AFTER_CHECK = 8           # 送出後幾秒回頭確認那一輪有沒有跑起來
CAP_PER_HOUR = 30         # 每個 agent 每小時最多被房間叫醒幾次
BACKOFF = {"unavailable": 600, "desktop_closed": 300, "race": 3, "error": 60, "detached": 60, "user_turn": 5}
DETACHED = "already has an active writer"
CLAUDE_HOME = os.path.expanduser(os.environ.get("ROOMS_CLAUDE_HOME", "~/.claude"))
CLAUDE_BIN = os.environ.get("ROOMS_CLAUDE_BIN", "").strip() or next(
    (p for p in (os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude", "/usr/bin/claude") if os.path.exists(p)), "claude")
# 重開 Claude 視窗的指令：{script} 換成要跑的 shell 腳本、{title} 換成視窗標題。沒設時在 WSL 用 Windows Terminal
# 開一個 WSL 分頁（發行版 ROOMS_WSL_DISTRO，預設 Ubuntu）；不是 WSL 又沒設就不開，只記錄
CLAUDE_OPEN = os.environ.get("ROOMS_CLAUDE_OPEN", "").strip()
WSL_DISTRO = os.environ.get("ROOMS_WSL_DISTRO", "").strip() or os.environ.get("WSL_DISTRO_NAME", "") or "Ubuntu"
# ROOMS_CLAUDE_HOMES：房間可以在背景開 Claude 成員的帳號（CLAUDE_CONFIG_DIR），格式同 ROOMS_CODEX_HOMES；預設只有 ~/.claude
CLAUDE_NAMES = {}
for _entry in (os.environ.get("ROOMS_CLAUDE_HOMES", "").strip() or "~/.claude").split(":"):
    if _entry.strip():
        _name, _, _path = _entry.strip().rpartition("=")
        _path = os.path.expanduser(_path)
        CLAUDE_NAMES[_name or os.path.basename(_path)] = _path
CLAUDE_MEMBERS = os.path.expanduser("~/.local/state/agent-rooms/claude-members")
REOPEN_GAP = 180          # 開過視窗後這段時間內不再開（等它起來、hook 接手）   # 視窗自己握著對話、沒接背景服務：daemon 確定沒收下


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
        if not QUOTA_HELPER:
            return True
        if QUOTA_HELPER not in sys.path:
            sys.path.append(QUOTA_HELPER)
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
    # 先問 daemon：剛開、還沒跑過第一輪的對話還沒有紀錄檔
    for home in ACCOUNTS:
        state, error = adapter("cli", _sock(home), {"method": "thread/state", "params": {"threadId": thread_id}})
        if not error and (state.get("threadRuntimeStatus") or {}).get("type") in ("idle", "active"):
            return "cli", _sock(home), f"{account_name(home)}（對話掛在這個帳號）"
    if not _found(WSL_SESSIONS, thread_id):
        return None
    for home in ACCOUNTS:
        if _has_quota(home):
            return "cli", _sock(home), f"{account_name(home)}（還有額度）"
    return "cli", None, "兩個 Codex 帳號的額度都用完了"


def account_name(home):
    return NAMES.get(home, os.path.basename(home))


def spawn(cwd, account=None):
    """在背景服務開一個新的 Codex 對話（沒有視窗）。

    account 指定帳號（名字）就只用那個；沒指定就照 ROOMS_SPAWN_ACCOUNTS 的順序挑還有額度的。
    回 (thread id, 帳號資料夾)；開不成丟 RuntimeError。模型、權限照那個帳號的設定，
    只有核准改成 never：沒有視窗就沒人能按核准，要核准的指令直接失敗、不會卡住。
    """
    by_name = {account_name(h): h for h in ACCOUNTS}
    if account:
        if account not in by_name:
            raise RuntimeError(f"沒有這個 Codex 帳號：{account}（有：{'、'.join(by_name)}）")
        homes = [by_name[account]]
    else:
        order = [by_name[n] for n in SPAWN_ORDER if n in by_name]
        homes = [h for h in order + [h for h in ACCOUNTS if h not in order] if _has_quota(h)]
        if not homes:
            raise RuntimeError("Codex 帳號的額度都用完了")
    errors = []
    for home in homes:
        got, error = adapter("cli", _sock(home), {"method": "thread/start", "params": {"cwd": cwd}})
        if not error:
            return got["threadId"], home
        errors.append(f"{account_name(home)}：{error}")
    raise RuntimeError("開不了新的 Codex 對話：" + "；".join(errors))


def window_holder(thread_id):
    """背景服務沒載入這個對話時，還有沒有程式開著它的紀錄檔（＝沒接上服務、自己獨立跑的視窗）。

    有的話回那個視窗的 CODEX_HOME（看它屬於哪個帳號），沒有回 None。
    """
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        proc = os.path.dirname(fd_dir)
        try:
            if "app-server-daemon" in os.readlink(os.path.join(proc, "exe")):
                continue                    # 背景服務自己開著的不算視窗
            if not any(thread_id in os.readlink(os.path.join(fd_dir, fd)) for fd in os.listdir(fd_dir)):
                continue
            with open(os.path.join(proc, "environ"), "rb") as fh:
                env = dict(v.split(b"=", 1) for v in fh.read().split(b"\0") if b"=" in v)
            return env.get(b"CODEX_HOME", b"~/.codex").decode()
        except OSError:
            continue
    return None


def window_open(thread_id):
    return window_holder(thread_id) is not None


def resume_hint(thread_id, home):
    """重開那個對話的指令；不是預設帳號就帶上 CODEX_HOME。"""
    home = (home or "~/.codex").replace(os.path.expanduser("~"), "~")
    prefix = "" if home in ("~/.codex", "") else f"CODEX_HOME={home} "
    return f"`{prefix}codex resume {thread_id}`"


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


def last_seen(session):
    """使用者最後一次在這個對話講話的時間（acc 的 last_seen，每次 UserPromptSubmit 都會更新）。"""
    try:
        with open(os.path.join(ACC_SESSIONS, session + ".json"), encoding="utf-8") as fh:
            return datetime.datetime.fromisoformat(json.load(fh)["last_seen"]).timestamp()
    except Exception:
        return 0


def user_active(session, last_sent):
    """使用者剛在這個對話講過話嗎？回還剩幾秒（0 是沒有）。"""
    seen = last_seen(session)
    if not seen:
        return 0
    if last_sent and abs(seen - last_sent) < OWN_ECHO:
        return 0                                  # 那是我們送進去的那一輪
    left = USER_IDLE - (time.time() - seen)
    return max(0, left)


def render(room, session, events):
    """送進 Codex 的內容：房間規則（它沒有房間 hook）＋這一批訊息。"""
    from rooms.cli import CODEX_RULES as RULES, line_for, maintainer_line
    me = next((m for m in room["members"] if m["session"] == session), {"name": session[:8]})
    rules = RULES.format(user=USER_NAME, user_id=USER, owner_label=OWNER_LABEL, room=room["name"], room_id=room["room_id"], name=me["name"], owner=room["owner_name"],
                         session=session, room_cmd=os.path.join(ROOT, "bin", "room"),
                         maintainer_line=maintainer_line(call("GET", "/api/overview")))
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
        self.headless = None      # 負責人或使用者要他沒開視窗也做事（room on／room spawn）
        self.held = None          # 這一批已經記過「等使用者那一輪」了
        self.user_seen = last_seen(session)   # 已經回報過的使用者最後一次在這個對話講話的時間

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
                self.check_user()
                got = call("GET", "/api/listen", session=self.session, wait=30, triggers_only=1,
                           consume=0, heartbeat=0)
                off = got.get("off")
                if off and last_seen(self.session) > off["at"] + OWN_ECHO:
                    #使用者回到這個對話打字了：標回線上，留著的訊息下一圈就補送
                    call("POST", "/api/off", session=self.session, on=0, **{"as": self.session})
                    self.note("online", "使用者回到這個對話了，標回線上，補送留著的訊息")
                    continue
                if not got["events"]:
                    continue
                room_id, events = got["room_id"], got["events"]
                self.headless = got.get("headless")
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

    def check_user(self):
        """使用者在這個 Codex 對話裡講話了（不是我們送進去的那一輪）：告訴核心他在場，互相 @ 的計數歸零。"""
        seen = last_seen(self.session)
        if seen > self.user_seen and not (self.last_sent and abs(seen - self.last_sent) < OWN_ECHO):
            got = call("POST", "/api/user_here", session=self.session)
            if got.get("resumed"):
                self.note("resumed", "使用者在這個對話講話了，房間解除暫停")
        self.user_seen = max(self.user_seen, seen)

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
        if status == "active" and user_active(self.session, self.last_sent):
            # 正在跑的是使用者剛開的那一輪：不插進去，等它跑完（閒下來）馬上送
            if self.held != events[-1]["seq"]:
                self.note("delayed", "使用者正在跟它講話，等那一輪跑完再送")
                self.held = events[-1]["seq"]
            time.sleep(BACKOFF["user_turn"])
            return
        if status == "active":
            method = "turn/steer"
        elif status == "idle":
            method = "turn/start"
        elif status == "notLoaded" and kind == "cli" and window_open(self.session):
            self.fail("detached", "那個 Codex 視窗沒接上房間（帶了 -c 這類參數開的視窗會自己獨立跑），訊息留著；"
                      f"關掉那個視窗、用 {resume_hint(self.session, window_holder(self.session))} 重開後會自動補送", events)
            time.sleep(BACKOFF["detached"])
            return
        elif status == "notLoaded" and kind == "cli" and self.headless:
            method = "turn/start"            # 轉接器會先在背景載入（thread/resume）再送
            why += "，背景載入（" + ("房間開的" if self.headless.get("spawned") else "被叫回來的") + "）"
        elif status == "notLoaded":
            # 視窗關了：使用者關掉就是不要它跑。標下線、訊息留著，他回到那個對話打字時才補送
            call("POST", "/api/off", session=self.session, on=1, reason="視窗關了", **{"as": self.session})
            self.note("offline", f"視窗關了，標成下線；{len(events)} 則訊息留著，使用者回到那個對話打字時補送")
            return
        else:
            self.fail("unavailable", f"對話狀態是 {status}（可能是額度用完），訊息留著，之後會再試", events)
            time.sleep(BACKOFF["unavailable"])
            return
        room = call("GET", f"/api/rooms/{room_id}", **{"as": self.session})
        text = render(room, self.session, events)
        params = {"threadId": self.session, "input": [{"type": "text", "text": text}]}
        if kind == "cli" and method == "turn/start" and (self.headless or {}).get("spawned"):
            params["sandbox"] = "workspace-write"     # 房間開的成員要能寫它的工作資料夾
        result, error = adapter(kind, sock, {"method": method, "params": params})
        if error and "No message sent" in error:
            log(self.session[:8], "狀態剛好變了，重試", error)
            time.sleep(BACKOFF["race"])
            return
        if error and DETACHED in error:
            self.fail("detached", "那個 Codex 視窗沒接上房間（帶了 -c 這類參數開的視窗會自己獨立跑），訊息留著；"
                      f"關掉那個視窗、用 {resume_hint(self.session, window_holder(self.session))} 重開後會自動補送", events)
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


def spawn_claude(cwd, account, model=None, effort=None, mode=None):
    """房間開的 Claude 成員：沒有視窗，每一批訊息由轉送程式跑一次 `claude -p` 接著同一個對話。

    這裡只記下設定（帳號、模型、推理強度、權限、工作資料夾），第一則訊息送進去時才真的開出對話。
    回 (session id, 帳號資料夾)。"""
    import uuid
    if account not in CLAUDE_NAMES:
        raise RuntimeError(f"沒有這個 Claude 帳號：{account}（有：{'、'.join(CLAUDE_NAMES)}；在 ROOMS_CLAUDE_HOMES 設）")
    session = str(uuid.uuid4())
    os.makedirs(CLAUDE_MEMBERS, exist_ok=True)
    member = {"account": account, "home": CLAUDE_NAMES[account], "cwd": cwd, "model": model, "effort": effort,
              "mode": mode or "auto", "started": False}
    with open(os.path.join(CLAUDE_MEMBERS, session + ".json"), "w", encoding="utf-8") as fh:
        json.dump(member, fh, ensure_ascii=False, indent=1)
    return session, CLAUDE_NAMES[account]


def claude_member(session):
    try:
        with open(os.path.join(CLAUDE_MEMBERS, session + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def claude_member_hint(session, member):
    return f"CLAUDE_CONFIG_DIR={member['home']} claude --resume {session}（在 {member['cwd']}）"


def claude_alive(session):
    """有沒有程式開著這個 Claude 對話：~/.claude/sessions/<pid>.json 記著 pid 與對話 id，pid 還在、啟動時間對得上才算。"""
    for path in glob.glob(os.path.join(CLAUDE_HOME, "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                info = json.load(fh)
            if info.get("sessionId") != session:
                continue
            with open(f"/proc/{info['pid']}/stat") as fh:
                started = fh.read().rsplit(")", 1)[1].split()[19]
            if not info.get("procStart") or str(info["procStart"]) == started:
                return True
        except (OSError, ValueError, KeyError, IndexError):
            continue
    # 剛開的視窗停在啟動畫面（例如信任資料夾的確認）時還沒登記，但程式已經在了
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as fh:
                args = fh.read().split(b"\0")
        except OSError:
            continue
        if b"--resume" in args and session.encode() in args and any(a.endswith(b"claude") for a in args[:2]):
            return True
    return False


def claude_transcript(session):
    """這台機器上（不是 Windows 桌面版）的對話紀錄：回傳 (紀錄路徑, 工作資料夾, 權限模式)；找不到回 None。"""
    found = glob.glob(os.path.join(CLAUDE_HOME, "projects", "*", session + ".jsonl"))
    if not found:
        return None
    cwd, mode = None, None
    with open(found[0], encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"cwd"' not in line and '"permission-mode"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "permission-mode":
                mode = row.get("permissionMode") or mode
            cwd = row.get("cwd") or cwd
    return found[0], cwd or os.path.expanduser("~"), mode


def open_claude(session, name, cwd, mode):
    """開一個新視窗接回這個 Claude 對話。成功回 None，不行就回原因。"""
    state = os.path.expanduser("~/.local/state/agent-rooms")
    os.makedirs(state, exist_ok=True)
    script = os.path.join(state, f"reopen-{session}.sh")
    prompt = f"{REOPEN_MARK}你的視窗關掉了，房間有人找你；新訊息附在下面，照平常處理。"
    args = [CLAUDE_BIN, "--resume", session] + (["--permission-mode", mode] if mode else []) + [prompt]
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/bash -l\n# 房間轉送程式產生：接回關掉的 Claude 對話\n"
                 f"cd {json.dumps(cwd)} || cd ~\nexec " + " ".join(json.dumps(a, ensure_ascii=False) for a in args) + "\n")
    os.chmod(script, 0o700)
    if CLAUDE_OPEN:
        command = CLAUDE_OPEN.format(script=script, title=name)
        subprocess.Popen(command, shell=True, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    home = paths.windows_home()
    if not home:
        return "不是 WSL，也沒設 ROOMS_CLAUDE_OPEN，不知道怎麼開視窗"
    wt = home / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "wt.exe"
    wsl = ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-l", script]
    command = [str(wt), "-w", "new", "--title", name] + wsl if wt.exists() else \
        ["/mnt/c/Windows/system32/cmd.exe", "/c", "start", name] + wsl
    subprocess.Popen(command, cwd="/mnt/c", start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


class ClaudeWorker(Worker):
    """替房裡的 Claude 盯著：平常什麼都不做（hook 會處理）；視窗關了又有人找它才開視窗接回。"""

    def run(self):
        opened, opened_seq = 0, 0
        while not self.stop:
            try:
                got = call("GET", "/api/listen", session=self.session, wait=30, triggers_only=1,
                           consume=0, heartbeat=0)
                if not got["events"] or self.stop:
                    continue
                if claude_alive(self.session):
                    time.sleep(15)              # 開著：Stop／PostToolUse hook 會送，這裡不插手
                    continue
                events = got["events"]
                if time.time() - opened < REOPEN_GAP:
                    time.sleep(15)
                    continue
                if opened_seq and events[-1]["seq"] <= opened_seq:
                    # 同一批已經開過一次視窗，沒接起來（被關掉、或沒按確認）：不再一直開，等有新的事再說
                    self.fail("reopen_failed", "開過視窗接回對話，但它沒有接起來（視窗被關掉、或停在確認畫面）；訊息留著，有新的事再開", events)
                    time.sleep(60)
                    continue
                found = claude_transcript(self.session)
                if not found:
                    time.sleep(60)              # 不在這台的紀錄裡（例如 Windows 桌面版）：交給它那邊的 hook
                    continue
                name = next((a["name"] for a in call("GET", "/api/overview")["agents"] if a["session"] == self.session), self.session[:8])
                why = open_claude(self.session, name, found[1], found[2])
                if why:
                    self.fail("desktop_closed", f"視窗關了，{why}；訊息留著", events)
                    time.sleep(BACKOFF["desktop_closed"])
                    continue
                opened, opened_seq = time.time(), events[-1]["seq"]
                self.reported = None
                self.note("reopen", f"視窗關了，開新視窗接回這個對話（{len(events)} 則等著）")
            except Exception as error:
                log(self.session[:8], "Claude 盯著時出錯", error)
                time.sleep(BACKOFF["error"])


class HeadlessClaudeWorker(Worker):
    """房間開的 Claude 成員：有新的一批就跑一次 `claude -p`（同一個對話接著講），跑完才收下一批。

    沒有人能按核准：權限照開成員時給的模式（預設 auto），房間指令另外放行，回話一定送得出去。"""

    def run(self):
        while not self.stop:
            try:
                got = call("GET", "/api/listen", session=self.session, wait=30, triggers_only=1,
                           consume=0, heartbeat=0)
                if not got["events"] or self.stop:
                    continue
                self.sent = [t for t in self.sent if time.time() - t < 3600]
                if len(self.sent) >= CAP_PER_HOUR:
                    self.note("capped", f"一小時內已叫醒 {CAP_PER_HOUR} 次，先停；訊息留著", light=True)
                    time.sleep(60)
                    continue
                self.turn(got["room_id"], got["events"])
            except Exception as error:
                log(self.session[:8], "Claude 成員出錯", error)
                time.sleep(BACKOFF["error"])

    def turn(self, room_id, events):
        member = claude_member(self.session)
        room = call("GET", f"/api/rooms/{room_id}", **{"as": self.session})
        text = render_claude(room, self.session, events)
        room_cmd = os.path.join(ROOT, "bin", "room")
        args = [CLAUDE_BIN, "-p", "--output-format", "json", "--permission-mode", member["mode"],
                "--allowedTools", f"Bash({room_cmd}:*)"]
        args += ["--resume", self.session] if member["started"] else ["--session-id", self.session]
        if member.get("model"):
            args += ["--model", member["model"]]
        if member.get("effort"):
            args += ["--effort", member["effort"]]
        env = dict(os.environ, ROOMS_DISPATCHED="1")
        env.pop("CLAUDE_CONFIG_DIR", None)
        if os.path.realpath(member["home"]) != os.path.realpath(os.path.expanduser("~/.claude")):
            env["CLAUDE_CONFIG_DIR"] = member["home"]     # 預設帳號不設：設了它會改到那個資料夾裡找 .claude.json
        # 先推進游標再跑（跟 Codex 一樣不自動重送）；跑的時候進來的訊息下一輪再送
        call("POST", "/api/ack", session=self.session, room_id=room_id, seq=events[-1]["seq"])
        call("POST", "/api/touch", session=self.session)
        self.last_sent = time.time()
        self.sent.append(self.last_sent)
        self.note("turn", f"背景跑 claude -p（{member['account']}）送了 {len(events)} 則（到 #{events[-1]['seq']}）")
        os.makedirs(os.path.join(CLAUDE_MEMBERS, "logs"), exist_ok=True)
        with open(os.path.join(CLAUDE_MEMBERS, "logs", self.session + ".jsonl"), "a", encoding="utf-8") as out:
            done = subprocess.run(args + [text], cwd=member["cwd"], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out.write(json.dumps({"at": time.time(), "seq": events[-1]["seq"], "code": done.returncode,
                                  "output": done.stdout[-20000:], "stderr": done.stderr[-5000:]}, ensure_ascii=False) + "\n")
        try:
            result = json.loads(done.stdout)
        except ValueError:
            result = {"is_error": True, "result": (done.stdout.strip() or done.stderr.strip())[-300:]}
        if not member["started"] and done.returncode == 0:
            member["started"] = True
            with open(os.path.join(CLAUDE_MEMBERS, self.session + ".json"), "w", encoding="utf-8") as fh:
                json.dump(member, fh, ensure_ascii=False, indent=1)
        call("POST", "/api/touch", session=self.session)
        if done.returncode or result.get("is_error"):
            self.fail("failed_after", f"那一輪沒跑完（{str(result.get('result') or '')[:200]}）；"
                      f"訊息已送過不重送，可用 {claude_member_hint(self.session, member)} 接上看", events)
            return
        self.reported = None
        log(self.session[:8], "那一輪跑完", f"{result.get('duration_ms', 0) / 1000:.0f} 秒")


def render_claude(room, session, events):
    """送進背景 Claude 的內容：房間規則（它沒有房間 hook，用命令列跟房間說話）＋這一批訊息。"""
    from rooms.cli import RULES, line_for, maintainer_line
    me = next((m for m in room["members"] if m["session"] == session), {"name": session[:8]})
    rules = RULES.format(user=USER_NAME, user_id=USER, owner_label=OWNER_LABEL, room=room["name"], room_id=room["room_id"],
                         name=me["name"], owner=room["owner_name"], session=session,
                         room_cmd=os.path.join(ROOT, "bin", "room"), maintainer_line=maintainer_line(call("GET", "/api/overview")))
    lines = [line_for(e, room["name"]) for e in events]
    return (rules + "\n- 你在背景跑、沒有視窗：回話一律用上面的 say 指令，直接寫在這裡的文字沒有人看得到。"
            "\n\n房間的新訊息：\n" + "\n".join(lines))


def main():
    workers = {}
    log("轉送程式啟動，核心", BASE)
    while True:
        try:
            agents = call("GET", "/api/overview")["agents"]
            wanted = {a["session"]: a["kind"] for a in agents if a["kind"] in ("codex", "claude") and a.get("room_id")}
            for session in wanted.keys() - workers.keys():
                kind = wanted[session]
                workers[session] = (Worker if kind == "codex" else
                                    HeadlessClaudeWorker if claude_member(session) else ClaudeWorker)(session)
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
