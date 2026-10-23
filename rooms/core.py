"""房間核心：房間、成員、移動紀錄、訊息、線頭、勿擾、任務。

只用標準函式庫。一個行程一把鎖：所有寫入都在 `self.cond` 裡做，
做完 `notify_all()` 叫醒正在監聽的人。

存放（`data/`）：
  state.json             房間、agent、任務、線頭的目前狀態（先寫暫存檔再 os.replace）
  rooms/<room_id>.jsonl  這間房的全部事件，一行一筆，seq 從 1 起
  moves.jsonl            每一次進出房間

身分：agent 用 session id（acc 的那個）；使用者 固定是 "user"。
"""

import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

USER = "user"
ONLINE_SECONDS = 75          # 監聽一輪最長 50 秒，再寬一點才算離線
MENTION = re.compile(r"@([\w\-\.㐀-鿿]+)")


class RoomError(Exception):
    """給呼叫端看的錯誤；HTTP 層轉成 4xx。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class Core:
    def __init__(self, data_dir):
        self.data = Path(data_dir)
        (self.data / "rooms").mkdir(parents=True, exist_ok=True)
        self.cond = threading.Condition()
        self.state = self._load()
        self.events = {}          # room_id -> list[event]，啟動時從 jsonl 讀回
        for room_id in self.state["rooms"]:
            path = self._events_path(room_id)
            rows = []
            if path.exists():
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.events[room_id] = rows

    # ── 存放 ─────────────────────────────────────────────

    def _load(self):
        path = self.data / "state.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"rooms": {}, "agents": {}, "tasks": {}, "threads": {}, "seq": {"room": 0, "task": 0, "thread": 0}}

    def _save(self):
        path = self.data / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _events_path(self, room_id):
        return self.data / "rooms" / f"{room_id}.jsonl"

    def _next(self, kind, prefix):
        self.state["seq"][kind] += 1
        return f"{prefix}{self.state['seq'][kind]}"

    def _emit(self, room_id, kind, actor, **fields):
        rows = self.events.setdefault(room_id, [])
        event = {"seq": len(rows) + 1, "at": _now(), "room_id": room_id, "type": kind,
                 "from": actor, "from_name": self.name_of(actor), **fields}
        rows.append(event)
        with self._events_path(room_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def _commit(self):
        self._save()
        self.cond.notify_all()

    # ── 查詢小工具 ───────────────────────────────────────

    def name_of(self, actor):
        if actor == USER:
            return "使用者"
        agent = self.state["agents"].get(actor)
        return agent["name"] if agent else actor

    def _room(self, room_id):
        room = self.state["rooms"].get(room_id)
        if not room:
            raise RoomError(f"沒有這間房：{room_id}", 404)
        return room

    def _agent(self, session):
        agent = self.state["agents"].get(session)
        if not agent:
            raise RoomError(f"這個 session 還沒登記：{session}", 404)
        return agent

    def _members(self, room_id):
        return [sid for sid, a in self.state["agents"].items() if a.get("room_id") == room_id]

    def _can_see(self, actor, room_id):
        """知識邊界：使用者 看得到每一間；agent 只看得到自己此刻所在的那一間。"""
        if actor == USER:
            return True
        agent = self.state["agents"].get(actor)
        return bool(agent) and agent.get("room_id") == room_id

    def _require_see(self, actor, room_id):
        self._room(room_id)
        if not self._can_see(actor, room_id):
            raise RoomError("你不在這間房，看不到它的內容", 403)

    def _is_room_owner(self, actor, room_id):
        return actor == USER or self._room(room_id)["owner"] == actor

    def _online(self, agent):
        seen = agent.get("listening_at")
        return bool(seen) and time.time() - seen < ONLINE_SECONDS

    # ── agent 登記 ──────────────────────────────────────

    def register(self, session, kind="", name=""):
        """登記或更新一個點。名字沒給就用 session 前 8 碼；之後 使用者 可以改。"""
        if not session or session == USER:
            raise RoomError("session id 不合法")
        with self.cond:
            agent = self.state["agents"].get(session)
            if agent is None:
                agent = {"session": session, "kind": kind, "name": name or session[:8],
                         "room_id": None, "dnd": False, "cursor": {}, "listening_at": 0,
                         "registered_at": _now()}
                self.state["agents"][session] = agent
            else:
                if kind:
                    agent["kind"] = kind
                if name:
                    self._check_name_free(name, session)
                    agent["name"] = name
            self._commit()
            return dict(agent)

    def _check_name_free(self, name, session):
        if name.lower() == USER:
            raise RoomError("user 這個名字保留給 使用者")
        for sid, a in self.state["agents"].items():
            if sid != session and a["name"] == name:
                raise RoomError(f"名字 {name} 已經有人用了")

    def rename(self, actor, session, name):
        if actor not in (USER, session):
            raise RoomError("只有 使用者 或本人能改名", 403)
        with self.cond:
            agent = self._agent(session)
            self._check_name_free(name, session)
            agent["name"] = name
            self._commit()
            return dict(agent)

    # ── 房間與移動 ──────────────────────────────────────

    def create_room(self, actor, name):
        if not name.strip():
            raise RoomError("房間要有名字")
        with self.cond:
            if actor != USER:
                self._agent(actor)
            room_id = self._next("room", "R")
            self.state["rooms"][room_id] = {"room_id": room_id, "name": name.strip(), "owner": actor,
                                            "created_at": _now(), "whiteboard": ""}
            self.events[room_id] = []
            self._emit(room_id, "room_created", actor, name=name.strip())
            self._commit()
            return dict(self.state["rooms"][room_id])

    def move(self, actor, session, room_id):
        """把 agent 移進某間房（room_id 為 None 就是離開）。一個 agent 同時只在一間。

        agent 可以移動自己；使用者 可以移動任何人（把點拖進房間）。
        """
        if actor not in (USER, session):
            raise RoomError("只有 使用者 或本人能移動這個 agent", 403)
        with self.cond:
            agent = self._agent(session)
            if room_id is not None:
                self._room(room_id)
            before = agent.get("room_id")
            if before == room_id:
                return dict(agent)
            if before:
                self._emit(before, "leave", session)
            agent["room_id"] = room_id
            if room_id:
                # 後進房的人看得到全部紀錄：cursor 從 0 起，第一次監聽就拿到開房以來的事
                agent["cursor"].setdefault(room_id, 0)
                self._emit(room_id, "join", session)
            with (self.data / "moves.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": _now(), "session": session, "by": actor,
                                     "from": before, "to": room_id}, ensure_ascii=False) + "\n")
            self._commit()
            return dict(agent)

    def moves(self, actor, session):
        if actor not in (USER, session):
            raise RoomError("只能查自己的移動紀錄", 403)
        path = self.data / "moves.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if r["session"] == session]

    def list_rooms(self, actor):
        """使用者 拿到全部；agent 只拿到自己所在那一間（知識邊界）。"""
        with self.cond:
            rooms = [r for rid, r in self.state["rooms"].items() if self._can_see(actor, rid)]
            return [self._room_view(r["room_id"]) for r in rooms]

    def _room_view(self, room_id):
        room = self._room(room_id)
        members = []
        for sid in self._members(room_id):
            a = self.state["agents"][sid]
            members.append({"session": sid, "name": a["name"], "kind": a["kind"],
                            "online": self._online(a), "dnd": a["dnd"],
                            "did": self._did(room_id, sid)})
        lit = [t for t in self.state["threads"].values()
               if t["room_id"] == room_id and not t.get("closed") and t["whose"] == USER]
        return {**room, "owner_name": self.name_of(room["owner"]), "members": members,
                "lit": len(lit)}

    def _did(self, room_id, session):
        """這個人在這間房做過什麼：發言數、交過的任務。"""
        posts = sum(1 for e in self.events.get(room_id, []) if e["type"] == "post" and e["from"] == session)
        submitted = [t["id"] for t in self.state["tasks"].values()
                     if t["room_id"] == room_id and any(h["by"] == session and h["action"] == "submit"
                                                        for h in t["history"])]
        return {"posts": posts, "submitted": submitted}

    def room(self, actor, room_id):
        with self.cond:
            self._require_see(actor, room_id)
            return self._room_view(room_id)

    def history(self, actor, room_id, since=0, limit=200):
        """這間房的事件。後進房的人一樣看得到全部，一次最多 limit 筆（大家同一個上限）。"""
        with self.cond:
            self._require_see(actor, room_id)
            rows = [e for e in self.events.get(room_id, []) if e["seq"] > since]
            return rows[:limit]

    # ── 白板 ─────────────────────────────────────────────

    def set_whiteboard(self, actor, room_id, text):
        with self.cond:
            self._require_see(actor, room_id)
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有房間負責人和 使用者 能改白板", 403)
            self.state["rooms"][room_id]["whiteboard"] = text
            self._emit(room_id, "whiteboard", actor, chars=len(text))
            self._commit()

    def whiteboard(self, actor, room_id):
        with self.cond:
            self._require_see(actor, room_id)
            return self.state["rooms"][room_id]["whiteboard"]

    # ── 訊息與線頭 ──────────────────────────────────────

    def _resolve_mentions(self, room_id, text):
        """@名字 → session（或 user）。只認此刻在房裡的人和 使用者。"""
        by_name = {self.state["agents"][sid]["name"]: sid for sid in self._members(room_id)}
        found = []
        for name in MENTION.findall(text):
            if name.lower() == USER:
                target = USER
            else:
                target = by_name.get(name)
            if target and target not in found:
                found.append(target)
        return found

    def post(self, actor, room_id, text):
        if not text.strip():
            raise RoomError("訊息不能是空的")
        with self.cond:
            self._require_see(actor, room_id)
            mentions = [m for m in self._resolve_mentions(room_id, text) if m != actor]
            # 他開口了，就是在回應：等他回話的線頭關掉（之後交給判官做更細的判斷）
            for thread in self.state["threads"].values():
                if thread["room_id"] == room_id and thread["whose"] == actor and not thread.get("closed") \
                        and thread["kind"] == "mention":
                    thread["closed"] = _now()
                    thread["closed_by"] = actor
            event = self._emit(room_id, "post", actor, text=text, mentions=mentions)
            for target in mentions:
                self._open_thread(room_id, target, actor, text, "mention", ref=event["seq"])
            self._commit()
            return event

    def _open_thread(self, room_id, whose, opener, text, kind, ref=None):
        # 同一個人、同一種、同一個來源只留一條，新的蓋舊的（another-project 的去重）
        for thread in self.state["threads"].values():
            if (thread["room_id"] == room_id and thread["whose"] == whose and thread["kind"] == kind
                    and thread["from"] == opener and not thread.get("closed")
                    and (kind == "mention" or thread.get("ref") == ref)):
                thread.update(text=text, ref=ref, updated=_now())
                return thread
        thread_id = self._next("thread", "L")
        thread = {"id": thread_id, "room_id": room_id, "whose": whose, "from": opener, "kind": kind,
                  "text": text, "ref": ref, "opened": _now(), "closed": None,
                  "needs_user": whose == USER}
        self.state["threads"][thread_id] = thread
        return thread

    def threads(self, actor, room_id, open_only=True):
        with self.cond:
            self._require_see(actor, room_id)
            return [t for t in self.state["threads"].values()
                    if t["room_id"] == room_id and (not open_only or not t.get("closed"))]

    def close_thread(self, actor, thread_id, note=""):
        with self.cond:
            thread = self.state["threads"].get(thread_id)
            if not thread:
                raise RoomError(f"沒有這條線頭：{thread_id}", 404)
            self._require_see(actor, thread["room_id"])
            if actor not in (USER, thread["whose"], thread["from"]):
                raise RoomError("只有等的人、開的人或 使用者 能關這條線頭", 403)
            thread["closed"] = _now()
            thread["closed_by"] = actor
            thread["note"] = note
            self._commit()
            return thread

    # ── 勿擾與監聽 ──────────────────────────────────────

    def set_dnd(self, actor, session, on):
        if actor not in (USER, session):
            raise RoomError("只有 使用者 或本人能切勿擾", 403)
        with self.cond:
            agent = self._agent(session)
            agent["dnd"] = bool(on)
            self._commit()
            return dict(agent)

    def pending(self, session):
        """還沒送給他的事件（不含他自己的）。勿擾中一律回空，紀錄留著等補送。"""
        agent = self._agent(session)
        room_id = agent.get("room_id")
        if not room_id or agent["dnd"]:
            return room_id, []
        cursor = agent["cursor"].get(room_id, 0)
        rows = [e for e in self.events.get(room_id, []) if e["seq"] > cursor and e["from"] != session]
        return room_id, rows

    def listen(self, session, wait=50.0, limit=200):
        """長輪詢：有新事件就回，沒有就等到 wait 秒。回傳後游標前進（送出即算已讀）。

        監聽本身就是心跳：畫面上的「在不在」看最後一次監聽的時間。
        """
        deadline = time.time() + max(0.0, min(wait, 55.0))
        with self.cond:
            self._agent(session)
            while True:
                agent = self.state["agents"][session]
                agent["listening_at"] = time.time()
                room_id, rows = self.pending(session)
                if rows:
                    rows = rows[:limit]
                    agent["cursor"][room_id] = rows[-1]["seq"]
                    self._save()
                    return {"room_id": room_id, "dnd": agent["dnd"], "events": rows}
                left = deadline - time.time()
                if left <= 0:
                    # 自己的事件也要推進游標，不然下次還會掃到它們
                    if room_id and not agent["dnd"] and self.events.get(room_id):
                        agent["cursor"][room_id] = self.events[room_id][-1]["seq"]
                    self._save()
                    return {"room_id": room_id, "dnd": agent["dnd"], "events": []}
                self.cond.wait(left)

    # ── 任務（照 acc 的 Objective；差別是由檢查者結案）─────────

    def _task(self, task_id):
        task = self.state["tasks"].get(task_id)
        if not task:
            raise RoomError(f"沒有這份任務：{task_id}", 404)
        return task

    def _task_log(self, task, actor, action, note="", **extra):
        task["history"].append({"at": _now(), "by": actor, "by_name": self.name_of(actor),
                                "action": action, "note": note, **extra})
        self._emit(task["room_id"], "task", actor, task_id=task["id"], action=action,
                   status=task["status"], objective=task["objective"], note=note)

    def create_task(self, actor, room_id, objective, criteria, verifier=None):
        with self.cond:
            self._require_see(actor, room_id)
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有房間負責人和 使用者 能發任務", 403)
            if not objective.strip() or not criteria.strip():
                raise RoomError("任務要寫 objective 和完成條件")
            verifier = verifier or self.state["rooms"][room_id]["owner"]
            if verifier != USER:
                self._agent(verifier)
            task_id = self._next("task", "T")
            task = {"id": task_id, "room_id": room_id, "objective": objective.strip(),
                    "criteria": criteria.strip(), "verifier": verifier, "status": "open",
                    "holder": None, "closed_by": None, "history": []}
            self.state["tasks"][task_id] = task
            self._task_log(task, actor, "create")
            self._commit()
            return dict(task)

    def tasks(self, actor, room_id):
        with self.cond:
            self._require_see(actor, room_id)
            return [t for t in self.state["tasks"].values() if t["room_id"] == room_id]

    def claim(self, actor, task_id):
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if actor == USER:
                raise RoomError("任務由 agent 領")
            if task["status"] not in ("open", "parked"):
                raise RoomError(f"{task_id} 現在是 {task['status']}，領不了", 409)
            task["status"] = "active"
            task["holder"] = actor
            self._task_log(task, actor, "claim")
            self._commit()
            return dict(task)

    def submit(self, actor, task_id, note):
        if not note.strip():
            raise RoomError("交件要寫 note：交了什麼、做了什麼、怎麼驗證的")
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if task["status"] != "active" or task["holder"] != actor:
                raise RoomError("只有領走這份任務的人能交件", 403)
            task["status"] = "review"
            self._task_log(task, actor, "submit", note)
            self._open_thread(task["room_id"], task["verifier"], actor,
                              f"{task_id} 已交、等待檢查：{task['objective']}", "review", ref=task_id)
            self._commit()
            return dict(task)

    def close_task(self, actor, task_id, result, note):
        if result not in ("achieved", "failed"):
            raise RoomError("result 只能是 achieved 或 failed")
        if not note.strip():
            raise RoomError("結案要寫 note：依據是什麼")
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if actor not in (USER, task["verifier"]):
                raise RoomError("只有這份任務的檢查者和 使用者 能結案", 403)
            if actor == task["holder"]:
                raise RoomError("不能檢查自己交的任務", 403)
            if task["status"] != "review":
                raise RoomError(f"{task_id} 現在是 {task['status']}，還不能結案", 409)
            task["status"] = result
            task["closed_by"] = actor
            self._task_log(task, actor, result, note)
            self._close_review_threads(task_id, actor)
            self._commit()
            return dict(task)

    def _close_review_threads(self, task_id, actor):
        for thread in self.state["threads"].values():
            if thread["kind"] == "review" and thread.get("ref") == task_id and not thread.get("closed"):
                thread["closed"] = _now()
                thread["closed_by"] = actor

    def park_task(self, actor, task_id, reason):
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if task["status"] != "active":
                raise RoomError(f"{task_id} 現在是 {task['status']}，不能暫停", 409)
            if actor not in (USER, task["holder"]) and not self._is_room_owner(actor, task["room_id"]):
                raise RoomError("只有做的人、負責人和 使用者 能暫停", 403)
            task["status"] = "parked"
            task["holder"] = None
            self._task_log(task, actor, "park", reason)
            self._commit()
            return dict(task)

    def revise_task(self, actor, task_id, objective, criteria, reason):
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if actor not in (USER, task["verifier"]) and not self._is_room_owner(actor, task["room_id"]):
                raise RoomError("只有檢查者、負責人和 使用者 能改寫任務", 403)
            if task["status"] in ("achieved", "failed", "dropped"):
                raise RoomError("已結案的任務不改寫，另發一份新的", 409)
            before = {"objective": task["objective"], "criteria": task["criteria"]}
            task["objective"] = objective.strip() or task["objective"]
            task["criteria"] = criteria.strip() or task["criteria"]
            self._task_log(task, actor, "revise", reason, before=before)
            self._commit()
            return dict(task)

    def drop_task(self, actor, task_id, reason):
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if not self._is_room_owner(actor, task["room_id"]):
                raise RoomError("只有負責人和 使用者 能取消任務", 403)
            if task["status"] in ("achieved", "failed", "dropped"):
                raise RoomError(f"{task_id} 已經結案", 409)
            task["status"] = "dropped"
            task["holder"] = None
            task["closed_by"] = actor
            self._task_log(task, actor, "drop", reason)
            self._close_review_threads(task_id, actor)
            self._commit()
            return dict(task)

    def amend_task(self, actor, task_id, to, reason):
        """更正先前的結案判斷（acc 的 amend）。"""
        if to not in ("achieved", "failed", "review"):
            raise RoomError("只能改成 achieved、failed 或 review")
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if actor not in (USER, task["verifier"]):
                raise RoomError("只有檢查者和 使用者 能更正結案", 403)
            if task["status"] not in ("achieved", "failed"):
                raise RoomError(f"{task_id} 還沒結案", 409)
            before = task["status"]
            task["status"] = to
            if to == "review":
                task["closed_by"] = None
            self._task_log(task, actor, "amend", reason, before=before)
            self._commit()
            return dict(task)

    # ── 使用者 的總覽 ─────────────────────────────────────

    def overview(self):
        with self.cond:
            agents = []
            for sid, a in self.state["agents"].items():
                agents.append({"session": sid, "name": a["name"], "kind": a["kind"],
                               "room_id": a.get("room_id"), "online": self._online(a), "dnd": a["dnd"]})
            return {"rooms": [self._room_view(rid) for rid in self.state["rooms"]], "agents": agents}

