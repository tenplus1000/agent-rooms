"""房間核心：房間、成員、移動紀錄、訊息、線頭、勿擾、任務。

只用標準函式庫。一個行程一把鎖：所有寫入都在 `self.cond` 裡做，
做完 `notify_all()` 叫醒正在監聽的人。

存放（`data/`）：
  state.json             房間、agent、任務、線頭的目前狀態（先寫暫存檔再 os.replace）
  rooms/<room_id>.jsonl  這間房的全部事件，一行一筆，seq 從 1 起
  moves.jsonl            每一次進出房間

身分：agent 用 session id（acc 的那個）；使用者是 identity.USER（由設定決定）。
"""

import datetime
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from rooms import judge
from rooms.identity import OWNER_LABEL, USER, USER_NAME

# 全域角色：不待在房裡、每間房都看得到、管得動人和房間（不能用使用者的身分發言）。
#   maintainer：改房間工具本身；被 @、房間暫停、訊息送不到時叫醒它
#   resident  ：常駐型代理（掌握全局、派房，不改程式）；被 @、有人 @使用者、房間暫停時叫醒它
GLOBAL_KINDS = {
    "maintainer": ("mention", "paused", "delivery"),
    "resident": ("mention", "user", "paused"),
}
ONLINE_SECONDS = 75          # 監聽一輪最長 50 秒，再寬一點才算離線
MAX_HOPS = int(os.environ.get("ROOMS_MAX_HOPS", "60"))   # agent 之間連續互相 @ 幾次就暫停（agentchattr 的 loop guard）；每間房可另設，0＝不限
JUDGE_CONTEXT = 4             # 判官看這則之前最近幾則發言（看得出誰派的工作）
def paused_text(limit):
    return (f"房間暫停了：agent 之間連續互相 @ 超過 {limit} 次（{USER_NAME} 一直沒出現），之後的訊息照記但不叫醒任何人。"
            f"{USER_NAME} 在房裡、或在任何一個成員自己的對話裡講一句話就恢復；請負責人告訴 {USER_NAME}。"
            f"這間房常需要長時間互傳的話，可以用 hops 調高上限或設成 off。")
EVERYONE = ("all", "全部", "大家")
MENTION = re.compile(r"@([\w\-\.㐀-鿿]+)")
# 畫面上房間是等角立方體（寬、深 150，高 100，cos 30°／sin 30°）。兩間的位置差剛好是其中一面的距離就算「接上」：
# 地板四個方向加上下疊，共六面。網頁拖近時會吸到這些位置。
_CW, _CD, _CH, _COS, _SIN = 150, 150, 100, 0.866, 0.5
NEIGHBOR_SIDES = {"右後": (_CW * _COS, _CW * _SIN), "左前": (-_CW * _COS, -_CW * _SIN),
                  "左後": (-_CD * _COS, _CD * _SIN), "右前": (_CD * _COS, -_CD * _SIN),
                  "上": (0, -_CH), "下": (0, _CH)}
NEIGHBOR_SLACK = 2.0


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
            return USER_NAME
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

    def _globals(self):
        """{session: {"kind", "alias"}}。"""
        return self.state.setdefault("globals", {})

    def _woken_on(self, why):
        """這種事要叫醒哪些全域角色（why：paused、delivery、user）。"""
        return [sid for sid, g in self._globals().items() if why in GLOBAL_KINDS.get(g["kind"], ())]

    def _admin(self, actor):
        """使用者或全域角色：管得動任何人、任何房間。全域角色發言就是它自己，不能用使用者的身分。"""
        return actor == USER or (bool(actor) and actor in self._globals())

    def _can_see(self, actor, room_id):
        """知識邊界：使用者和維修看得到每一間；agent 只看得到自己此刻所在的那一間。"""
        if self._admin(actor):
            return True
        agent = self.state["agents"].get(actor)
        return bool(agent) and agent.get("room_id") == room_id

    def _require_see(self, actor, room_id):
        self._room(room_id)
        if not self._can_see(actor, room_id):
            raise RoomError("你不在這間房，看不到它的內容", 403)

    def _is_room_owner(self, actor, room_id):
        return self._admin(actor) or self._room(room_id)["owner"] == actor

    def _online(self, agent):
        seen = agent.get("listening_at")
        return bool(seen) and time.time() - seen < ONLINE_SECONDS

    # ── agent 登記 ──────────────────────────────────────

    def register(self, session, kind="", name="", spawned_by=None):
        """登記或更新一個點。名字沒給就用 session 前 8 碼；之後使用者可以改。

        spawned_by：這個對話是房間替人在背景開的（沒有視窗），轉送程式可以直接載入它送訊息。
        """
        if not session or session == USER:
            raise RoomError("session id 不合法")
        with self.cond:
            agent = self.state["agents"].get(session)
            if agent is None:
                agent = {"session": session, "kind": kind, "name": name or session[:8],
                         "room_id": None, "dnd": False, "cursor": {}, "listening_at": 0,
                         "registered_at": _now()}
                self.state["agents"][session] = agent
                if spawned_by:
                    agent["headless"] = {"at": time.time(), "by": spawned_by, "spawned": True}
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
            raise RoomError(f"{USER} 這個名字保留給使用者")
        for sid, a in self.state["agents"].items():
            if sid != session and a["name"] == name:
                raise RoomError(f"名字 {name} 已經有人用了")

    def rename(self, actor, session, name):
        if not self._admin(actor) and actor != session:
            raise RoomError("只有使用者、維修或本人能改名", 403)
        with self.cond:
            agent = self._agent(session)
            self._check_name_free(name, session)
            agent["name"] = name
            self._commit()
            return dict(agent)

    # ── 房間與移動 ──────────────────────────────────────

    def create_room(self, actor, name, owner=None):
        """開房。開的人就是負責人；使用者或維修可以指定別人當負責人，自己不進去。"""
        if not name.strip():
            raise RoomError("房間要有名字")
        with self.cond:
            if actor != USER:
                self._agent(actor)
            if owner and owner != actor:
                if not self._admin(actor):
                    raise RoomError("只有使用者或維修能替房間指定別的負責人", 403)
                if owner != USER:
                    self._agent(owner)
            room_id = self._next("room", "R")
            self.state["rooms"][room_id] = {"room_id": room_id, "name": name.strip(), "owner": owner or actor,
                                            "created_at": _now(), "whiteboard": ""}
            self.events[room_id] = []
            self._emit(room_id, "room_created", actor, name=name.strip())
            self._commit()
            return dict(self.state["rooms"][room_id])

    def delete_room(self, actor, room_id, evict=False):
        """刪房間（使用者或維修；常駐代理不能刪）。房裡要先沒人；紀錄搬到 rooms/deleted/ 封存，不真的刪。

        evict：房裡的人全部下線時，先把他們移出房間再刪（畫面上確認過才送）。
        """
        with self.cond:
            if actor != USER and self._globals().get(actor, {}).get("kind") != "maintainer":
                raise RoomError("只有使用者或維修能刪房間", 403)
            room = self._room(room_id)
            if evict:
                awake = [self.name_of(m) for m in self._members(room_id) if not self.state["agents"][m].get("off")]
                if awake:
                    raise RoomError(f"{room['name']} 裡還有沒下線的人：{'、'.join(awake)}")
                for m in self._members(room_id):
                    self.move(actor, m, None)
            if self._members(room_id):
                raise RoomError(f"{room['name']} 裡還有人：先把成員移出去（move <名字> none）再刪")
            archive = self.data / "rooms" / "deleted"
            archive.mkdir(parents=True, exist_ok=True)
            source = self._events_path(room_id)
            if source.exists():
                source.rename(archive / f"{room_id}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
            for thread in self.state["threads"].values():
                if thread["room_id"] == room_id and not thread.get("closed"):
                    thread.update(closed=_now(), closed_by=actor, note="房間刪掉了")
            del self.state["rooms"][room_id]
            self.events.pop(room_id, None)
            with (self.data / "moves.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": _now(), "deleted_room": room_id, "name": room["name"], "by": actor},
                                    ensure_ascii=False) + "\n")
            self._commit()
            return {"room_id": room_id, "deleted": room["name"]}

    def set_room_pos(self, actor, room_id, x, y):
        """畫面上房間擺在哪（使用者、全域角色或負責人拖的）。接上或拉開別間時，兩邊房裡都記一筆。"""
        with self.cond:
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有使用者、全域角色或負責人能搬房間", 403)
            before = set(self.neighbors(room_id))
            self._room(room_id)["pos"] = [float(x), float(y)]
            after = set(self.neighbors(room_id))
            for other in sorted(after - before):
                for a, b in ((room_id, other), (other, room_id)):
                    self._emit(a, "linked", actor, other=b, other_name=self.state["rooms"][b]["name"])
            for other in sorted(before - after):
                for a, b in ((room_id, other), (other, room_id)):
                    self._emit(a, "unlinked", actor, other=b, other_name=self.state["rooms"][b]["name"])
            self._commit()
            return {"room_id": room_id, "pos": self.state["rooms"][room_id]["pos"]}

    def set_owner(self, actor, room_id, session):
        """換負責人：使用者或全域角色；新的負責人要在房裡。"""
        with self.cond:
            if not self._admin(actor):
                raise RoomError("只有使用者或全域角色能換負責人", 403)
            room = self._room(room_id)
            if session != USER and session not in self._members(room_id):
                raise RoomError(f"{self.name_of(session)} 不在這間房，先把他移進來")
            before = room["owner"]
            room["owner"] = session
            self._emit(room_id, "owner", actor, owner=session, owner_name=self.name_of(session),
                       before_name=self.name_of(before), wake=[session] if session not in (actor, USER) else [])
            self._commit()
            return self._room_view(room_id)

    def desk(self, actor, session):
        """全域角色的專屬管道：使用者不選任何房間時跟它講話的地方。第一次用才開，之後都是同一間。"""
        with self.cond:
            if session not in self._globals():
                raise RoomError(f"{self.name_of(session)} 不是全域角色")
            if actor not in (USER, session):
                raise RoomError("只有使用者和那個全域角色本人能用它的專屬管道", 403)
            for room_id, room in self.state["rooms"].items():
                if room.get("desk") == session:
                    return dict(room)
            room_id = self._next("room", "R")
            self.state["rooms"][room_id] = {"room_id": room_id, "name": f"{self.name_of(session)} 的專屬管道",
                                            "owner": USER, "created_at": _now(), "whiteboard": "", "desk": session}
            self.events[room_id] = []
            self._emit(room_id, "room_created", actor, name=self.state["rooms"][room_id]["name"])
            self._commit()
            return dict(self.state["rooms"][room_id])

    def set_hops(self, actor, room_id, limit):
        """互相 @ 的上限：負責人、使用者或維修；0＝不限（同時解除暫停）。"""
        limit = int(limit)
        if limit < 0:
            raise RoomError("上限要是 0（不限）或正整數")
        with self.cond:
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有使用者、維修或負責人能改互相 @ 的上限", 403)
            room = self._room(room_id)
            room["max_hops"] = limit
            if room.get("paused") and (not limit or room.get("hops", 0) <= limit):
                room["paused"] = False
                for thread in self.state["threads"].values():
                    if thread["room_id"] == room_id and thread["kind"] == "paused" and not thread.get("closed"):
                        thread.update(closed=_now(), closed_by=actor, note=f"{self.name_of(actor)} 調整上限")
            self._commit()
            return {"room_id": room_id, "max_hops": limit, "hops": room.get("hops", 0), "paused": bool(room.get("paused"))}

    def rename_room(self, actor, room_id, name):
        """改房名：負責人或使用者。"""
        if not name.strip():
            raise RoomError("房間要有名字")
        with self.cond:
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有負責人或使用者能改房名")
            room = self._room(room_id)
            before = room["name"]
            room["name"] = name.strip()
            self._emit(room_id, "room_renamed", actor, name=name.strip(), before=before)
            self._commit()
            return dict(room)

    def move(self, actor, session, room_id):
        """把 agent 移進某間房（room_id 為 None 就是離開）。一個 agent 同時只在一間。

        agent 可以移動自己；使用者可以移動任何人（把點拖進房間）。
        """
        if not self._admin(actor) and actor != session:
            raise RoomError("只有使用者、維修或本人能移動這個 agent", 403)
        with self.cond:
            agent = self._agent(session)
            if room_id is not None:
                self._room(room_id)
            before = agent.get("room_id")
            if before == room_id:
                return dict(agent)
            if before:
                self._answered(before, session, "離開房間")
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
        """使用者拿到全部；agent 只拿到自己所在那一間（知識邊界）。"""
        with self.cond:
            rooms = [r for rid, r in self.state["rooms"].items() if self._can_see(actor, rid)]
            return [self._room_view(r["room_id"]) for r in rooms]

    def _room_view(self, room_id):
        room = self._room(room_id)
        members = []
        for sid in self._members(room_id):
            a = self.state["agents"][sid]
            members.append({"session": sid, "name": a["name"], "kind": a["kind"],
                            "online": self._online(a), "dnd": a["dnd"], "off": a.get("off"), "role": a.get("role", ""),
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
                raise RoomError("只有房間負責人和使用者能改白板", 403)
            self.state["rooms"][room_id]["whiteboard"] = text
            self._emit(room_id, "whiteboard", actor, chars=len(text))
            for other in self.neighbors(room_id):          # 接上的房也看得到這塊白板：告訴他們改了
                self._emit(other, "whiteboard", actor, chars=len(text), neighbor=room_id,
                           neighbor_name=self.state["rooms"][room_id]["name"])
            self._commit()

    def whiteboard(self, actor, room_id):
        with self.cond:
            self._require_see(actor, room_id)
            return self.state["rooms"][room_id]["whiteboard"]

    def neighbors(self, room_id):
        """畫面上跟這間接上的房（六面任一面貼著）：{房號: 哪一面}。專屬管道、沒擺過位置的不算。"""
        room = self._room(room_id)
        if not room.get("pos") or room.get("desk"):
            return {}
        x, y = room["pos"]
        found = {}
        for rid, other in self.state["rooms"].items():
            if rid == room_id or not other.get("pos") or other.get("desk"):
                continue
            dx, dy = other["pos"][0] - x, other["pos"][1] - y
            for side, (sx, sy) in NEIGHBOR_SIDES.items():
                if abs(dx - sx) <= NEIGHBOR_SLACK and abs(dy - sy) <= NEIGHBOR_SLACK:
                    found[rid] = side
        return found

    def neighbor_boards(self, actor, room_id):
        """接上的房的白板：看得到，改不了（各自只改自己的）。"""
        with self.cond:
            self._require_see(actor, room_id)
            return [{"room_id": rid, "name": self.state["rooms"][rid]["name"], "side": side,
                     "owner_name": self.name_of(self.state["rooms"][rid]["owner"]),
                     "whiteboard": self.state["rooms"][rid]["whiteboard"]}
                    for rid, side in self.neighbors(room_id).items()]

    def hosts(self, actor):
        """各房的負責人（給負責人找別房負責人傳訊用）：房號、房名、負責人名字。"""
        with self.cond:
            return [{"room_id": rid, "name": r["name"], "owner_name": self.name_of(r["owner"]) if r.get("owner") else None}
                    for rid, r in self.state["rooms"].items() if not r.get("desk")]

    # ── 訊息與線頭 ──────────────────────────────────────

    def _resolve_mentions(self, room_id, text):
        """@名字 → session（或使用者）。只認此刻在房裡的人和使用者。"""
        by_name = {self.state["agents"][sid]["name"]: sid for sid in self._members(room_id)}
        for sid, g in self._globals().items():        # 全域角色不在房裡，用名字或叫法都找得到
            if sid in self.state["agents"]:
                by_name.setdefault(self.state["agents"][sid]["name"], sid)
                if g.get("alias"):
                    by_name.setdefault(g["alias"], sid)
        found = []
        for name in MENTION.findall(text):
            if name.lower() == USER:
                target = USER
            else:
                target = by_name.get(name)
            if target and target not in found:
                found.append(target)
        return found

    def _host_targets(self, actor, room_id, text):
        """負責人 @ 了別房的負責人（名字，或直接寫房號）：{對方 session: 對方房號}。"""
        here = set(self._members(room_id))
        owners = {}
        for rid, r in self.state["rooms"].items():
            if rid != room_id and not r.get("desk") and r.get("owner") and r["owner"] != USER \
                    and r["owner"] != actor and r["owner"] in self.state["agents"]:
                owners[self.state["agents"][r["owner"]]["name"]] = (r["owner"], rid)
                owners.setdefault(rid, (r["owner"], rid))
        found = {}
        for name in MENTION.findall(text):
            hit = owners.get(name) or owners.get(name.upper())
            if hit and hit[0] not in here and hit[0] not in self._globals():
                found.setdefault(*hit)
        return found

    def shelve_state(self, session, evidence):
        """給判官看的：這個下線對話是誰、在做什麼、為什麼下線、最後說了什麼（房間紀錄由核心補，對話紀錄由主機那邊帶來）。"""
        agent = self._agent(session)
        off = agent.get("off") or {}
        days = (time.time() - off.get("at", time.time())) / 86400
        kind = {"claude": "Claude", "codex": "Codex"}.get(agent["kind"], agent["kind"] or "agent")
        lines = [f"對話「{agent['name']}」（{kind}），已經下線 {days:.0f} 天。" if days >= 1 else
                 f"對話「{agent['name']}」（{kind}），剛下線（{days * 24:.0f} 小時內）。"]
        if agent.get("role"):
            lines.append(f"在做什麼：{agent['role']}")
        if off.get("reason"):
            lines.append(f"下線時記的原因：{off['reason']}（{self.name_of(off.get('by', ''))} 標的）")
        if evidence.get("objective"):
            lines.append(f"它自己的目標：{evidence['objective']}")
        posts = [e for rows in self.events.values() for e in rows if e["type"] == "post" and e["from"] == session]
        posts.sort(key=lambda e: e["at"])
        if posts:
            lines.append(f"它最後在房間說的話：「{posts[-1]['text'][:300]}」")
        if evidence.get("last"):
            lines.append(f"它的對話最後一段：「{evidence['last'][:300]}」")
        return "\n".join(lines)

    def judge_shelve(self, actor, session, evidence, key):
        """判官判這個下線的對話該不該收起來，結果記在它身上（畫面的整理區照這個建議）。同一個 key 判過就不再問。"""
        if actor and not self._admin(actor):
            raise RoomError("只有使用者或全域角色能請判官判", 403)
        with self.cond:
            agent = self._agent(session)
            if not agent.get("off") or agent.get("shelved"):
                return None
            hint = agent.get("shelve_hint") or {}
            if hint.get("key") == key and not hint.get("error"):
                return hint
            state = self.shelve_state(session, evidence)
        if not judge.enabled():
            return None
        probs, error = judge.shelve_hint(str(self.data / "judge.jsonl"), state)     # 不拿鎖：判官慢不卡房間
        with self.cond:
            agent = self._agent(session)
            if error:
                agent["shelve_hint"] = {"key": key, "error": error, "at": time.time()}
            else:
                why = (agent.get("off") or {}).get("reason") or evidence.get("objective") or evidence.get("last") or agent.get("role") or ""
                agent["shelve_hint"] = {"key": key, "at": time.time(), "state": state, "done": round(probs["done"], 3),
                                        "pending": round(probs["pending"], 3),
                                        "suggest": probs["done"] >= judge.THRESHOLD and probs["pending"] < judge.THRESHOLD,
                                        "why": why[:120]}
            self._commit()
            return agent["shelve_hint"]

    def _shelve_label(self, session, label, actor):
        """使用者對判官建議的處置就是標註：收起來＝判對（不用再叫回來），留著＝判錯，拿出來＝反悔。
        沒動作的不記（還沒確定）。每筆附判官當時看到的內容與機率，之後換模型或訓練時用。"""
        agent = self.state["agents"][session]
        hint = agent.get("shelve_hint") or {}
        row = {"at": _now(), "session": session, "name": agent["name"], "label": label, "by": actor,
               "suggested": hint.get("suggest"), "done": hint.get("done"), "pending": hint.get("pending"),
               "judged_at": hint.get("at"), "state": hint.get("state") or self._judged_state(agent["name"])}
        with (self.data / "shelve_labels.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _judged_state(self, name):
        """舊的建議沒存判官看到的內容：從 judge.jsonl 找最後一次判這個名字的那一發。"""
        found = None
        try:
            with (self.data / "judge.jsonl").open(encoding="utf-8") as fh:
                for line in fh:
                    if '"shelve"' in line and f"對話「{name}」" in line:
                        found = json.loads(line).get("state")
        except (OSError, ValueError):
            pass
        return found

    def keep_shelve_hint(self, actor, session):
        """使用者說留著：這次下線不再建議收起（下次再下線會重新判）。"""
        if actor and not self._admin(actor):
            raise RoomError("只有使用者或全域角色能改", 403)
        with self.cond:
            hint = self._agent(session).get("shelve_hint")
            if hint:
                self._shelve_label(session, "kept", actor)
                hint.update(suggest=False, kept=time.time())
                self._commit()
            return hint

    def set_role(self, actor, session, role):
        """成員在做什麼（一句話）。判官靠它判斷沒 @ 的訊息在找誰。使用者、負責人或本人可以改。"""
        with self.cond:
            agent = self._agent(session)
            room = self.state["rooms"].get(agent.get("room_id") or "")
            if not self._admin(actor) and actor != session and not (room and room["owner"] == actor):
                raise RoomError("只有使用者、維修、房間負責人或本人能改", 403)
            agent["role"] = role.strip()
            self._commit()
            return dict(agent)

    def _role_line(self, sid, room):
        agent = self.state["agents"][sid]
        doing = agent.get("role")
        if not doing:
            held = [t["objective"] for t in self.state["tasks"].values()
                    if t.get("holder") == sid and t["status"] == "active"]
            doing = "；".join(held) or "（沒寫在做什麼）"
        kind = {"claude": "Claude", "codex": "Codex"}.get(agent["kind"], agent["kind"] or "agent")
        return kind + ("，房間負責人" if room["owner"] == sid else "") + "：" + doing

    def judge_wake(self, actor, room_id, text):
        """成員發言時問判官：除了被 @ 的人，還有誰該被叫醒（呼叫時不拿鎖，判官慢不會卡住整個房間）。

        回 {"probs": {名字: 機率}} 或 {"error": 原因}；不用問（使用者講話、@all、判官關著、沒有別人可問）回 None。
        """
        with self.cond:
            self._require_see(actor, room_id)
            # 使用者講話照規則給負責人；只有負責人不在房裡（例如使用者自己開的房）才要問判官
            if not text.strip() or not judge.enabled():
                return None
            if actor == USER and (self.state["rooms"][room_id]["owner"] in self._members(room_id)
                                  or self._resolve_mentions(room_id, text)):
                return None
            if any(n.lower() in EVERYONE for n in MENTION.findall(text)):
                return None
            room = self.state["rooms"][room_id]
            members = self._members(room_id)
            mentioned = [m for m in self._resolve_mentions(room_id, text) if m in members]
            roles = [(self.name_of(sid), self._role_line(sid, room)) for sid in members]
            targets = [self.name_of(sid) for sid in members if sid != actor and sid not in mentioned]
            posts = [e for e in self.events.get(room_id, []) if e["type"] == "post"][-JUDGE_CONTEXT:]
            context = [(e["from_name"], e["text"][:200]) for e in posts]
            speaker, room_name = self.name_of(actor), room["name"]
        if not targets:
            return None
        call = uuid.uuid4().hex[:12]
        probs, error = judge.addressees(str(self.data / "judge.jsonl"), room_name, roles, speaker, text, targets,
                                        [self.name_of(m) for m in mentioned], context, room_id=room_id, call=call)
        return {"error": error, "call": call} if probs is None else {"probs": probs, "call": call}

    def post(self, actor, room_id, text, judged=None):
        if not text.strip():
            raise RoomError("訊息不能是空的")
        with self.cond:
            self._require_see(actor, room_id)
            room = self.state["rooms"][room_id]
            mentions = [m for m in self._resolve_mentions(room_id, text) if m != actor]
            relays = self._host_targets(actor, room_id, text) if room["owner"] == actor else {}
            # 他開口了，就是在回應（之後交給判官做更細的判斷）
            self._answered(room_id, actor, "發言")
            members = [m for m in self._members(room_id) if m != actor]
            everyone = any(n.lower() in EVERYONE for n in MENTION.findall(text))
            wake = [m for m in members if everyone or m in mentions]
            desk = room.get("desk")
            if desk and actor != desk and desk not in wake:
                wake.append(desk)                     # 專屬管道：使用者講的話都是給那個全域角色的
            for sid in self._globals():
                if sid in mentions and sid not in members and sid != actor:
                    wake.append(sid)                  # 全域角色不在房裡，@ 它照樣叫醒
            if actor == USER:
                room["hops"] = 0
                room["paused"] = False
                if not mentions and not everyone and room["owner"] in members:
                    wake.append(room["owner"])       # 使用者沒點名就是對負責人說
                elif not mentions and not everyone and not desk:
                    # 負責人不在房裡（例如使用者自己開的房）：判官判過就照判官，叫不到就叫房裡還在線上的人
                    if judged and judged.get("probs") is not None:
                        wake = [m for m in members if judged["probs"].get(self.name_of(m), 0) >= judge.THRESHOLD]
                    else:
                        wake = [m for m in members if not self.state["agents"][m].get("off")]
            else:
                named = [m for m in mentions if m in members]          # @ 到的房裡成員（不含使用者、維修）
                if judged and judged.get("probs") is not None:
                    # 判官判過：被 @ 的照叫，其他人過門檻的也叫（例如派工的人、事情會碰到的人）
                    wake += [m for m in members if m not in wake
                             and judged["probs"].get(self.name_of(m), 0) >= judge.THRESHOLD]
                elif not named and not everyone and USER not in mentions and room["owner"] in members:
                    wake.append(room["owner"])       # 判官叫不到：退回對負責人說，不然負責人看不到
                if USER in mentions and not named and room["owner"] in members and room["owner"] not in wake:
                    wake.append(room["owner"])       # 只 @使用者：使用者不在房裡，要靠負責人轉給他
            if USER in mentions:                     # 等使用者的事：常駐型代理要知道
                wake += [sid for sid in self._woken_on("user") if sid != actor and sid not in wake]
            just_paused = False
            if actor != USER and wake:
                room["hops"] = room.get("hops", 0) + 1
                limit = room.get("max_hops", MAX_HOPS)
                if limit and room["hops"] > limit and not room.get("paused"):
                    room["paused"] = just_paused = True
                    self._open_thread(room_id, USER, actor, paused_text(limit), "paused")
                if room.get("paused"):
                    wake = []                       # 暫停中：訊息照記，不叫醒人
            held = bool(room.get("paused")) and actor != USER
            extra = {"judged": judged} if judged else {}
            event = self._emit(room_id, "post", actor, text=text, mentions=mentions, wake=wake, held=held, **extra)
            for target in mentions:
                self._open_thread(room_id, target, actor, text, "mention", ref=event["seq"])
            relayed = []
            for target, other in relays.items():
                # 負責人找別房的負責人：送到對方房（對方房裡的人都看得到），叫醒那位負責人
                note = f"〔從 {room['name']}（{room_id}）{OWNER_LABEL} {self.name_of(actor)} 傳來〕"
                got = self._emit(other, "post", actor, text=note + text, mentions=[target], wake=[target],
                                 held=False, via=room_id)
                self._open_thread(other, target, actor, text, "mention", ref=got["seq"])
                relayed.append(f"{self.name_of(target)}（{other}）")
            if just_paused and (room["owner"] in members or self._woken_on("paused")):
                # 負責人要知道房間停了（發話的人收到的 notice 不會轉告別人），由他告訴使用者；只叫這一次
                self._emit(room_id, "paused", actor, text=paused_text(limit),
                           wake=[w for w in [room["owner"]] + self._woken_on("paused") if w])
            self._commit()
            if relayed:
                event = dict(event, relayed=relayed)
            if held:
                # 發話的人要知道：訊息記下了，但沒有人被叫醒
                return dict(event, notice="房間暫停中：這則已記下，但沒有叫醒任何人；使用者在房裡講一句話才恢復。")
            off = [self.name_of(m) for m in wake if self.state["agents"][m].get("off")]
            if off:
                return dict(event, notice=f"{'、'.join(off)} 已下線（做完了或視窗關了），不會被叫醒；"
                                          "訊息留著，回到線上時會補給他。")
            return event

    def set_global(self, actor, session, kind, alias=""):
        """使用者指定全域角色（kind 見 GLOBAL_KINDS；kind 空字串＝取消）。alias 是房裡 @ 它的另一個叫法。"""
        if actor != USER:
            raise RoomError("只有使用者能指定全域角色", 403)
        if kind and kind not in GLOBAL_KINDS:
            raise RoomError(f"沒有這種全域角色：{kind}（有：{'、'.join(GLOBAL_KINDS)}）")
        with self.cond:
            agent = self._agent(session)
            found = self._globals()
            if not kind:
                found.pop(session, None)
                self._commit()
                return {"session": session, "removed": True}
            alias = alias.strip()
            for sid, g in found.items():
                if sid != session and alias and alias == g.get("alias"):
                    raise RoomError(f"叫法「{alias}」已經給了別人")
            if session not in found:
                # 從現在起才算：以前各房的事件不補給新的全域角色
                for room_id, rows in self.events.items():
                    agent["cursor"][room_id] = max(agent["cursor"].get(room_id, 0), len(rows))
            found[session] = {"kind": kind, "alias": alias}
            self._commit()
            return {"session": session, "name": agent["name"], **found[session]}

    # ── 遠端鑰匙：從網路連進來的請求靠它決定身分（本機的入口不用）──────────

    def issue_key(self, actor, identity, label=""):
        """發一把鑰匙給某個身分（使用者，或已登記的 agent／全域角色）。只存雜湊；原文只在這次回傳。"""
        import secrets
        if actor != USER:
            raise RoomError("只有使用者能發鑰匙", 403)
        with self.cond:
            if identity != USER:
                self._agent(identity)
            token = secrets.token_urlsafe(32)
            self.state.setdefault("keys", {})[hashlib.sha256(token.encode()).hexdigest()] = {
                "identity": identity, "label": label, "created": _now()}
            self._commit()
            return {"identity": identity, "name": self.name_of(identity), "key": token}

    def revoke_keys(self, actor, identity):
        if actor != USER:
            raise RoomError("只有使用者能收回鑰匙", 403)
        with self.cond:
            keys = self.state.setdefault("keys", {})
            gone = [h for h, k in keys.items() if k["identity"] == identity]
            for h in gone:
                del keys[h]
            self._commit()
            return {"identity": identity, "revoked": len(gone)}

    def identity_for_key(self, token):
        """鑰匙 → 身分；對不上回 None。"""
        if not token:
            return None
        row = self.state.get("keys", {}).get(hashlib.sha256(token.encode()).hexdigest())
        return row["identity"] if row else None

    def resume(self, actor, room_id):
        """解除暫停（使用者、維修或負責人）：互相 @ 的計數歸零。"""
        with self.cond:
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有使用者、維修或負責人能解除暫停", 403)
            room = self._room(room_id)
            was = bool(room.get("paused"))
            room["hops"] = 0
            room["paused"] = False
            for thread in self.state["threads"].values():
                if thread["room_id"] == room_id and thread["kind"] == "paused" and not thread.get("closed"):
                    thread.update(closed=_now(), closed_by=actor, note=f"{self.name_of(actor)} 解除暫停")
            self._commit()
            return {"room_id": room_id, "resumed": was}

    def user_here(self, session):
        """使用者在某個成員自己的對話裡講話了：等同他在那個成員所在的房間，互相 @ 的計數歸零、暫停解除。"""
        with self.cond:
            agent = self._agent(session)
            room = self.state["rooms"].get(agent.get("room_id") or "")
            if not room:
                return {}
            was_paused = bool(room.get("paused"))
            room["hops"] = 0
            room["paused"] = False
            for thread in self.state["threads"].values():     # 只收掉「暫停」那條；@使用者的問題他還沒回
                if thread["room_id"] == room["room_id"] and thread["kind"] == "paused" and not thread.get("closed"):
                    thread.update(closed=_now(), closed_by=USER, note=f"使用者在 {agent['name']} 的對話裡講話")
            self._commit()
            return {"room_id": room["room_id"], "resumed": was_paused}

    def _open_thread(self, room_id, whose, opener, text, kind, ref=None):
        # 同一個人、同一種、同一個來源只留一條，新的蓋舊的
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

    def _answered(self, room_id, session, note):
        """他在這間房有動作了（發言、領任務、交件），或離開了：等他回話的線頭關掉。"""
        for thread in self.state["threads"].values():
            if thread["room_id"] == room_id and thread["whose"] == session and not thread.get("closed") \
                    and thread["kind"] in ("mention", "paused"):
                thread["closed"] = _now()
                thread["closed_by"] = session
                thread["note"] = note

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
                raise RoomError("只有等的人、開的人或使用者能關這條線頭", 403)
            thread["closed"] = _now()
            thread["closed_by"] = actor
            thread["note"] = note
            self._commit()
            return thread

    # ── 勿擾與監聽 ──────────────────────────────────────

    def set_dnd(self, actor, session, on):
        if not self._admin(actor) and actor != session:
            raise RoomError("只有使用者、維修或本人能切勿擾", 403)
        with self.cond:
            agent = self._agent(session)
            agent["dnd"] = bool(on)
            self._commit()
            return dict(agent)

    def shelve(self, actor, session, shelved):
        """收起來：使用者或維修整理下線的人，畫面上不再列在狀態列與 @ 名單；紀錄都留著。
        要收先得下線、也不能在房裡；他回到線上（自己回來或被叫回來）就自動拿出來。"""
        if not self._admin(actor):
            raise RoomError("只有使用者或維修能整理", 403)
        with self.cond:
            agent = self._agent(session)
            if shelved:
                if not agent.get("off"):
                    raise RoomError(f"{agent['name']} 還沒下線，先下線再收")
                if agent.get("room_id"):
                    raise RoomError(f"{agent['name']} 還在 {agent['room_id']}，先移出房間再收")
                if not agent.get("shelved"):
                    self._shelve_label(session, "shelved", actor)
                agent["shelved"] = {"at": time.time(), "by": actor}
            else:
                if agent.get("shelved"):
                    self._shelve_label(session, "unshelved", actor)
                agent.pop("shelved", None)
            self._commit()
            return {"session": session, "name": agent["name"], "shelved": bool(agent.get("shelved"))}

    def set_off(self, actor, session, on, reason=""):
        """下線：做完了、或視窗關了。下線的人不會被叫醒（不替他跑一輪、不花額度），訊息照留；
        回到線上時，留著的就一起補送。使用者、他所在房間的負責人、或本人可以切。

        別人（使用者、負責人）把他標回線上，就是要他沒開視窗也起來做事：記成 headless，
        轉送程式會在背景載入那個對話送進去。本人回來（使用者回到那個視窗打字）不算。"""
        with self.cond:
            agent = self._agent(session)
            room = self.state["rooms"].get(agent.get("room_id") or "")
            if not self._admin(actor) and actor != session and not (room and room["owner"] == actor):
                raise RoomError("只有使用者、維修、房間負責人或本人能切下線", 403)
            if on:
                agent.pop("headless", None)
            elif actor != session:
                agent["headless"] = {"at": time.time(), "by": actor}
            if bool(on) == bool(agent.get("off")):
                self._commit()
                return dict(agent)
            agent["off"] = {"at": time.time(), "by": actor, "reason": reason} if on else None
            if not on:
                agent.pop("shelved", None)                # 回到線上就從收起來裡拿出來
                agent.pop("shelve_hint", None)
            if room:
                self._emit(room["room_id"], "off" if on else "on", session, by=actor, by_name=self.name_of(actor),
                           reason=reason)
            self._commit()
            return dict(agent)

    def pending(self, session):
        """還沒送給他的事件（不含他自己的）。勿擾中一律回空，紀錄留著等補送。"""
        agent = self._agent(session)
        room_id = agent.get("room_id")
        if agent["dnd"]:
            return room_id, []
        rows = []
        if room_id:
            cursor = agent["cursor"].get(room_id, 0)
            rows = [e for e in self.events.get(room_id, []) if e["seq"] > cursor and e["from"] != session]
        if session in self._globals():
            # 全域角色不用待在房裡：別的房間裡找它的事件（被 @、暫停、送不到……）也算它的
            for other, events in self.events.items():
                if other == room_id:
                    continue
                cursor = agent["cursor"].get(other, 0)
                rows += [e for e in events if e["seq"] > cursor and session in e.get("wake", ())]
        return room_id, rows

    @staticmethod
    def _advance(agent, rows):
        for e in rows:
            agent["cursor"][e["room_id"]] = max(agent["cursor"].get(e["room_id"], 0), e["seq"])

    def listen(self, session, wait=50.0, limit=200, triggers_only=False, consume=True, heartbeat=True):
        """長輪詢：有新事件就回，沒有就等到 wait 秒。

        triggers_only：只有出現「該叫醒他」的事件（event["wake"] 有他）才回，
          回的時候把累積的背景一起帶上；沒有就繼續等，背景留著不動。
        consume：回傳後游標前進（送出即算已讀）。轉送程式用 False，送成功再 ack。
        heartbeat：監聽本身就是心跳，畫面上的「在不在」看最後一次監聽的時間。
          轉送程式替 Codex 監聽時用 False，在不在由它另外回報。
        """
        deadline = time.time() + max(0.0, min(wait, 55.0))
        with self.cond:
            self._agent(session)
            while True:
                agent = self.state["agents"][session]
                if heartbeat:
                    agent["listening_at"] = time.time()
                room_id, rows = self.pending(session)
                # 下線的人不叫醒：叫醒的事件留著，回到線上時才算數
                ready = rows and (not triggers_only or (not agent.get("off")
                                                        and any(session in e.get("wake", ()) for e in rows)))
                if ready:
                    rows = rows[:limit]
                    if consume:
                        self._advance(agent, rows)
                    self._save()
                    return {"room_id": room_id, "dnd": agent["dnd"], "off": agent.get("off"),
                            "headless": agent.get("headless"), "events": rows}
                left = deadline - time.time()
                if left <= 0:
                    # 只剩他自己的事件時推進游標，不然下次還會掃到它們
                    if consume and not triggers_only and room_id and not agent["dnd"] and self.events.get(room_id):
                        agent["cursor"][room_id] = self.events[room_id][-1]["seq"]
                    self._save()
                    return {"room_id": room_id, "dnd": agent["dnd"], "off": agent.get("off"),
                            "headless": agent.get("headless"), "events": []}
                self.cond.wait(left)

    def touch(self, session):
        """轉送程式確認 Codex 那邊找得到這個對話時，替它記一次心跳。"""
        with self.cond:
            self._agent(session)["listening_at"] = time.time()
            self._save()

    def ack(self, session, room_id, seq):
        """轉送程式送成功後才把游標推到 seq。"""
        with self.cond:
            agent = self._agent(session)
            current = agent["cursor"].get(room_id, 0)
            agent["cursor"][room_id] = max(current, int(seq))
            self._save()
            return agent["cursor"][room_id]

    def rewind(self, actor, session, room_id, seq):
        """把游標倒回 seq（使用者或全域角色用）：轉送程式誤把沒跑成的一批記成送到時，讓那一批重送。"""
        if actor and not self._admin(actor):          # 沒帶身分＝本機的轉送程式（撞額度時倒回那一批）
            raise RoomError("只有使用者或全域角色能倒回游標", 403)
        with self.cond:
            agent = self._agent(session)
            agent["cursor"][room_id] = min(agent["cursor"].get(room_id, 0), int(seq))
            self._commit()
            return agent["cursor"][room_id]

    def record_delivery(self, session, action, detail="", light=False, notify=()):
        """每一次叫醒、插話、延後、擋下都記一筆；light=True 時在他所在的房間亮燈。

        notify：送不到時要讓誰知道（通常是找他的人）。會在房裡留一則「送不到」並叫醒這些人，
        不然找人的那一方只會以為對方沒回。
        """
        with self.cond:
            agent = self._agent(session)
            row = {"at": _now(), "session": session, "name": agent["name"], "room_id": agent.get("room_id"),
                   "action": action, "detail": detail}
            with (self.data / "deliveries.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            agent["delivery"] = {"at": row["at"], "action": action, "detail": detail}
            if light and agent.get("room_id"):
                self._open_thread(agent["room_id"], USER, session, f"{agent['name']}：{detail}", "delivery",
                                  ref=action)
            if notify and agent.get("room_id"):
                members = self._members(agent["room_id"])
                wake = [m for m in notify if m in members and m != session]
                for sid in self._woken_on("delivery"):  # 多半是房間工具本身的問題，維修直接修
                    if sid != session and sid not in wake:
                        wake.append(sid)
                self._emit(agent["room_id"], "delivery", session, action=action, detail=detail, wake=wake)
            self._commit()
            return row

    def deliveries(self, limit=200):
        path = self.data / "deliveries.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rows[-limit:]

    # ── 任務（照 acc 的 Objective；差別是由檢查者結案）─────────

    def _task(self, task_id):
        task = self.state["tasks"].get(task_id)
        if not task:
            raise RoomError(f"沒有這份任務：{task_id}", 404)
        return task

    def _task_log(self, task, actor, action, note="", prev_holder=None, **extra):
        task["history"].append({"at": _now(), "by": actor, "by_name": self.name_of(actor),
                                "action": action, "note": note, **extra})
        # 這件事要叫醒誰：新任務叫醒房裡其他人來領；交件叫醒檢查者；
        # 結案、改寫、取消、更正叫醒做的人。領走、暫停只是記錄。
        if action in ("claim", "submit", "park"):
            self._answered(task["room_id"], actor, f"任務 {task['id']} {action}")
        members = [m for m in self._members(task["room_id"]) if m != actor]
        holder = task["holder"] or prev_holder
        if action == "create":
            wake = members
        elif action == "submit":
            wake = [task["verifier"]]
        elif action in ("achieved", "failed", "revise", "drop", "amend"):
            wake = [holder] if holder else []
        else:
            wake = []
        wake = [w for w in wake if w in members]
        self._emit(task["room_id"], "task", actor, task_id=task["id"], action=action,
                   status=task["status"], objective=task["objective"], note=note,
                   holder=holder, verifier=task["verifier"], wake=wake)

    def create_task(self, actor, room_id, objective, criteria, verifier=None):
        with self.cond:
            self._require_see(actor, room_id)
            if not self._is_room_owner(actor, room_id):
                raise RoomError("只有房間負責人和使用者能發任務", 403)
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
                raise RoomError("只有這份任務的檢查者和使用者能結案", 403)
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
                raise RoomError("只有做的人、負責人和使用者能暫停", 403)
            task["status"] = "parked"
            prev = task["holder"]
            task["holder"] = None
            self._task_log(task, actor, "park", reason, prev_holder=prev)
            self._commit()
            return dict(task)

    def revise_task(self, actor, task_id, objective, criteria, reason):
        with self.cond:
            task = self._task(task_id)
            self._require_see(actor, task["room_id"])
            if actor not in (USER, task["verifier"]) and not self._is_room_owner(actor, task["room_id"]):
                raise RoomError("只有檢查者、負責人和使用者能改寫任務", 403)
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
                raise RoomError("只有負責人和使用者能取消任務", 403)
            if task["status"] in ("achieved", "failed", "dropped"):
                raise RoomError(f"{task_id} 已經結案", 409)
            task["status"] = "dropped"
            prev = task["holder"]
            task["holder"] = None
            task["closed_by"] = actor
            self._task_log(task, actor, "drop", reason, prev_holder=prev)
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
                raise RoomError("只有檢查者和使用者能更正結案", 403)
            if task["status"] not in ("achieved", "failed"):
                raise RoomError(f"{task_id} 還沒結案", 409)
            before = task["status"]
            task["status"] = to
            if to == "review":
                task["closed_by"] = None
            self._task_log(task, actor, "amend", reason, before=before)
            self._commit()
            return dict(task)

    # ──使用者的總覽 ─────────────────────────────────────

    def overview(self):
        with self.cond:
            agents = []
            for sid, a in self.state["agents"].items():
                agents.append({"session": sid, "name": a["name"], "kind": a["kind"],
                               "room_id": a.get("room_id"), "online": self._online(a), "dnd": a["dnd"],
                               "off": a.get("off"), "headless": bool(a.get("headless")),
                               "role": a.get("role", ""), "seen": a.get("listening_at") or 0,
                               "delivery": a.get("delivery"), "shelved": a.get("shelved"),
                               "shelve_hint": a.get("shelve_hint")})
            rooms = []
            for rid in self.state["rooms"]:
                view = self._room_view(rid)
                posts = [e for e in self.events.get(rid, []) if e["type"] in ("post", "paused", "delivery")]
                view["recent"] = [{"seq": e["seq"], "at": e["at"], "from": e["from_name"], "type": e["type"],
                                   "text": (e.get("text") or e.get("detail") or "")[:120]} for e in posts[-3:]]
                view["last_at"] = (self.events.get(rid) or [{}])[-1].get("at")
                view["neighbors"] = self.neighbors(rid)
                rooms.append(view)
            return {"rooms": rooms, "agents": agents, "globals": dict(self._globals()),
                    "user": {"id": USER, "name": USER_NAME}, "labels": {"owner": OWNER_LABEL}}

