import os
os.environ["ROOMS_JUDGE"] = "off"          # 測試不打真的判官；判官另外 mock
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

from rooms import cli, dispatch
from rooms.core import MAX_HOPS, USER, USER_NAME, Core
from rooms.identity import OWNER_LABEL
from rooms.server import make_handler


class WakeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = Core(self._tmp.name)
        for sid, name in (("s-boss", "主持"), ("s-a", "a"), ("s-b", "b")):
            self.core.register(sid, "codex", name)
        self.room = self.core.create_room("s-boss", "房")["room_id"]
        for sid in ("s-boss", "s-a", "s-b"):
            self.core.move(sid, sid, self.room)
            self.core.listen(sid, wait=0)

    def tearDown(self):
        self._tmp.cleanup()

    def wake_of(self, event):
        return sorted(event["wake"])

    def test_mentions_wake_only_those_named(self):
        self.assertEqual(self.wake_of(self.core.post("s-boss", self.room, "@a 看白板")), ["s-a"])
        self.assertEqual(self.wake_of(self.core.post("s-boss", self.room, "隨口說說")), [])
        self.assertEqual(self.wake_of(self.core.post("s-boss", self.room, "@all 集合")), ["s-a", "s-b"])

    def test_member_without_mention_reports_to_the_owner(self):
        self.assertEqual(self.wake_of(self.core.post("s-a", self.room, "我做完了")), ["s-boss"])
        self.assertEqual(self.wake_of(self.core.post("s-a", self.room, "@b 我做完了")), ["s-b"])
        self.assertEqual(self.core.post("s-boss", self.room, "大家辛苦")["wake"], [])   # 負責人自己沒點名就是背景

    def judged_post(self, actor, text, probs=None, error=None):
        with mock.patch.dict(os.environ, {"ROOMS_JUDGE": "on", "ROOMS_JUDGE_KEY": "k"}), \
                mock.patch("rooms.judge.addressees", return_value=(probs, error)) as ask:
            judged = self.core.judge_wake(actor, self.room, text)
        return self.core.post(actor, self.room, text, judged=judged), ask

    def test_judge_wakes_whoever_the_message_is_for(self):
        self.core.set_role("s-boss", "s-b", "從尾端支援 a")
        event, ask = self.judged_post("s-a", "請 b 停止支援", {"主持": 0.55, "b": 0.93})
        self.assertEqual(self.wake_of(event), ["s-b", "s-boss"])
        roles = dict(ask.call_args[0][2])
        self.assertIn("從尾端支援 a", roles["b"])                       # 判官看得到每個人在做什麼
        self.assertIn("房間負責人", roles["主持"])
        self.assertEqual(ask.call_args[0][5], ["主持", "b"])             # 不問說話的人

    def test_judge_says_nobody_means_background(self):
        event, _ = self.judged_post("s-a", "我去喝杯水", {"主持": 0.16, "b": 0.08})
        self.assertEqual(event["wake"], [])

    def test_judge_unreachable_falls_back_to_owner(self):
        event, _ = self.judged_post("s-a", "做完了", None, "URLError: timed out")
        self.assertEqual(event["wake"], ["s-boss"])
        self.assertIn("error", event["judged"])

    def test_user_in_a_room_without_its_owner_reaches_someone(self):
        room = self.core.create_room(USER, "使用者的房")["room_id"]
        for sid in ("s-a", "s-b"):
            self.core.move(sid, sid, room)
        self.core.set_off(USER, "s-b", True)
        self.assertEqual(self.core.post(USER, room, "為什麼這邊沒有主持")["wake"], ["s-a"])    # 判官關著：叫還在的人
        with mock.patch.dict(os.environ, {"ROOMS_JUDGE": "on", "ROOMS_JUDGE_KEY": "k"}), \
                mock.patch("rooms.judge.addressees", return_value=({"a": 0.2, "b": 0.9}, None)):
            judged = self.core.judge_wake(USER, room, "b 你那邊好了嗎")
        self.assertEqual(self.core.post(USER, room, "b 你那邊好了嗎", judged=judged)["wake"], ["s-b"])

    def test_user_skips_the_judge(self):
        _, ask = self.judged_post(USER, "大家好", {})
        self.assertFalse(ask.called)

    def test_judge_adds_people_a_mention_affects(self):
        self.core.post("s-boss", self.room, "@b 你先做自己的題")
        event, ask = self.judged_post("s-boss", "@a 開始幫 b，從尾端往前判", {"b": 0.79})
        self.assertEqual(self.wake_of(event), ["s-a", "s-b"])            # @ 的照叫，事情碰到的 b 也叫
        self.assertEqual(ask.call_args[0][5], ["b"])                      # 只問沒被 @ 的
        self.assertEqual(ask.call_args[0][6], ["a"])                      # 告訴判官 @ 了誰
        self.assertIn(("主持", "@b 你先做自己的題"), ask.call_args[0][7])  # 也給它看最近的對話

    def test_mentioned_only_when_judge_says_nobody_else(self):
        event, _ = self.judged_post("s-boss", "@a 先把你那份判完", {"b": 0.07})
        self.assertEqual(self.wake_of(event), ["s-a"])
        event, _ = self.judged_post("s-boss", "@a 先把你那份判完", None, "URLError: timed out")
        self.assertEqual(self.wake_of(event), ["s-a"])                    # 判官叫不到也不亂叫負責人

    def test_report_to_user_reaches_the_owner(self):
        event, _ = self.judged_post("s-a", f"@{USER} 重算完成，結果在 out.txt", {"b": 0.1, "主持": 0.42})
        self.assertEqual(self.wake_of(event), ["s-boss"])                 # 使用者 不在房裡，靠負責人轉

    def test_user_without_mention_talks_to_the_owner(self):
        self.assertEqual(self.wake_of(self.core.post(USER, self.room, "這個怎麼樣")), ["s-boss"])
        self.assertEqual(self.wake_of(self.core.post(USER, self.room, "@b 你來")), ["s-b"])

    def test_task_events_wake_the_right_people(self):
        task = self.core.create_task("s-boss", self.room, "我要做 A", "測試過")["id"]
        events = lambda: [e for e in self.core.events[self.room] if e["type"] == "task"]
        self.assertEqual(self.wake_of(events()[-1]), ["s-a", "s-b"])          # 新任務：大家來領
        self.core.claim("s-a", task)
        self.assertEqual(events()[-1]["wake"], [])                            # 領走：只記錄
        self.core.submit("s-a", task, "交了")
        self.assertEqual(events()[-1]["wake"], ["s-boss"])                    # 交件：檢查者
        self.core.close_task("s-boss", task, "failed", "少一項")
        self.assertEqual(events()[-1]["wake"], ["s-a"])                       # 結案：做的人

    def test_drop_wakes_the_previous_holder(self):
        task = self.core.create_task("s-boss", self.room, "我要做 B", "測試過")["id"]
        self.core.claim("s-b", task)
        self.core.drop_task("s-boss", task, "不做了")
        self.assertEqual(self.core.events[self.room][-1]["wake"], ["s-b"])

    def test_listen_triggers_only_waits_and_brings_context(self):
        self.core.post("s-boss", self.room, "背景一")
        self.assertEqual(self.core.listen("s-a", wait=0, triggers_only=True)["events"], [])
        self.core.post("s-b", self.room, "背景二")
        self.core.post("s-boss", self.room, "@a 該你了")
        got = self.core.listen("s-a", wait=0, triggers_only=True)["events"]
        self.assertEqual([e["text"] for e in got if e["type"] == "post"], ["背景一", "背景二", "@a 該你了"])

    def test_peek_then_ack(self):
        self.core.post("s-boss", self.room, "@a 一")
        first = self.core.listen("s-a", wait=0, triggers_only=True, consume=False)["events"]
        again = self.core.listen("s-a", wait=0, triggers_only=True, consume=False)["events"]
        self.assertEqual(first, again)
        self.core.ack("s-a", self.room, first[-1]["seq"])
        self.assertEqual(self.core.listen("s-a", wait=0, triggers_only=True)["events"], [])

    def test_dispatcher_listen_is_not_presence(self):
        self.core.state["agents"]["s-a"]["listening_at"] = 0
        self.core.listen("s-a", wait=0, heartbeat=False)
        member = next(m for m in self.core.room(USER, self.room)["members"] if m["session"] == "s-a")
        self.assertFalse(member["online"])

    def test_agents_pinging_each_other_get_paused_until_user_speaks(self):
        for i in range(MAX_HOPS):
            self.assertTrue(self.core.post("s-a" if i % 2 == 0 else "s-b", self.room,
                                           "@b 換你" if i % 2 == 0 else "@a 換你")["wake"])
        held = self.core.post("s-a", self.room, "@b 再換你")
        self.assertEqual(held["wake"], [])
        self.assertTrue(held["held"])
        self.assertIn("暫停", held["notice"])                           # 發話的人知道被擋了
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)
        self.core.post(USER, self.room, "好了，b 繼續")
        self.assertEqual(self.core.post("s-a", self.room, "@b 繼續")["wake"], ["s-b"])
        self.assertEqual(self.core.room(USER, self.room)["lit"], 0)

    def pause(self):
        for i in range(MAX_HOPS):
            self.core.post("s-a" if i % 2 == 0 else "s-b", self.room, "@b 換你" if i % 2 == 0 else "@a 換你")
        return self.core.post("s-a", self.room, "@b 再換你")

    def test_pause_wakes_the_owner_once(self):
        self.pause()
        paused = [e for e in self.core.events[self.room] if e["type"] == "paused"]
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["wake"], ["s-boss"])                 # 負責人知道，好轉告 使用者
        self.core.post("s-b", self.room, "@a 還在嗎")
        self.assertEqual(len([e for e in self.core.events[self.room] if e["type"] == "paused"]), 1)

    def test_user_talking_in_a_members_own_chat_resumes(self):
        self.pause()
        self.assertTrue(self.core.user_here("s-boss")["resumed"])
        self.assertEqual(self.core.post("s-a", self.room, "@b 繼續")["wake"], ["s-b"])
        self.assertEqual(self.core.room(USER, self.room)["lit"], 0)
        self.core.post("s-a", self.room, f"@{USER} 這個要你決定")
        self.core.user_here("s-a")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)  # @使用者 的問題他還沒回，燈留著

    def keeper(self):
        self.core.register("s-m", "claude", "修房")
        self.core.set_global(USER, "s-m", "maintainer", "維修")
        return "s-m"

    def test_only_user_names_the_maintainer(self):
        self.core.register("s-m", "claude", "修房")
        with self.assertRaises(Exception):
            self.core.set_global("s-boss", "s-m", "maintainer")

    def test_mention_wakes_the_maintainer_outside_the_room(self):
        self.keeper()
        for text in ("@維修 叫不醒工人", "@修房 叫不醒工人"):
            event = self.core.post("s-a", self.room, text)
            self.assertIn("s-m", event["wake"])
        got = self.core.listen("s-m", wait=0, triggers_only=True)
        self.assertEqual([e["text"] for e in got["events"]], ["@維修 叫不醒工人", "@修房 叫不醒工人"])
        self.assertEqual(self.core.listen("s-m", wait=0, triggers_only=True)["events"], [])   # 讀過就不再給
        self.core.post("s-a", self.room, "跟維修無關的話")
        self.assertEqual(self.core.listen("s-m", wait=0, triggers_only=False)["events"], [])  # 不在房裡：只收找它的

    def test_pause_and_failed_delivery_wake_the_maintainer(self):
        self.keeper()
        self.pause()
        self.core.record_delivery("s-a", "uncertain", "送出後結果不確定", light=True, notify=["s-boss"])
        kinds = [e["type"] for e in self.core.listen("s-m", wait=0, triggers_only=True)["events"]]
        self.assertEqual(kinds, ["paused", "delivery"])

    def test_maintainer_manages_any_room_but_speaks_as_itself(self):
        m = self.keeper()
        self.assertEqual(len(self.core.list_rooms(m)), 1)                  # 不在房裡也看得到
        self.core.move(m, "s-a", None)                                     # 移得動別人
        self.core.set_off(m, "s-b", True, "做完了")
        room = self.core.create_room(m, "新房", owner="s-a")
        self.assertEqual(room["owner"], "s-a")                             # 開房指定主持，自己不進去
        self.assertIsNone(self.core.state["agents"][m].get("room_id"))
        event = self.core.post(m, self.room, "修好了")
        self.assertEqual(event["from"], m)                                 # 發言是它自己，不是 使用者
        with self.assertRaises(Exception):
            self.core.set_global(m, "s-a", "resident")                     # 不能自己指定全域角色
        with self.assertRaises(Exception):
            self.core.create_room("s-b", "別人的房", owner="s-a")            # 一般成員不能替人指定主持

    def test_resident_hears_calls_for_the_user_not_tool_failures(self):
        self.keeper()
        self.core.register("s-r", "claude", "管家")
        self.core.set_global(USER, "s-r", "resident", "總管")
        self.assertIn("s-r", self.core.post("s-a", self.room, "@總管 今天先做哪個")["wake"])
        self.assertIn("s-r", self.core.post("s-a", self.room, f"@{USER} 這要你決定")["wake"])     # 在等使用者
        self.core.record_delivery("s-a", "uncertain", "送出後結果不確定", light=True, notify=["s-boss"])
        self.pause()
        got = [e["type"] for e in self.core.listen("s-r", wait=0, triggers_only=True)["events"]]
        self.assertEqual(got, ["post", "post", "paused"])                       # 送不到是維修的事
        kinds = [e["type"] for e in self.core.listen("s-m", wait=0, triggers_only=True)["events"]]
        self.assertIn("delivery", kinds)
        self.assertNotIn(f"@{USER} 這要你決定", [e.get("text") for e in
                                             self.core.listen("s-m", wait=0)["events"]])   # 維修不收等使用者的事

    def test_global_roles_are_set_by_the_user_only_and_can_be_removed(self):
        self.keeper()
        self.core.register("s-r", "claude", "管家")
        with self.assertRaises(Exception):
            self.core.set_global(USER, "s-r", "resident", "維修")              # 叫法不能重複
        with self.assertRaises(Exception):
            self.core.set_global(USER, "s-r", "boss")                           # 只有兩種
        self.core.set_global(USER, "s-m", "")
        self.assertNotIn("s-m", self.core.post("s-a", self.room, "@維修 在嗎")["wake"])  # 取消後叫不到

    def test_desk_is_a_private_line_to_a_global_role(self):
        m = self.keeper()
        desk = self.core.desk(USER, m)
        self.assertEqual(self.core.desk(USER, m)["room_id"], desk["room_id"])   # 同一間
        event = self.core.post(USER, desk["room_id"], "幫我看一下 R1")
        self.assertEqual(event["wake"], [m])
        self.assertEqual(self.core.post(m, desk["room_id"], "看完了")["wake"], [])  # 它回話不用叫誰
        with self.assertRaises(Exception):
            self.core.desk("s-a", m)

    def test_owner_and_position_are_set_from_the_screen(self):
        self.core.set_room_pos(USER, self.room, 120, 80)
        self.assertEqual(self.core.room(USER, self.room)["pos"], [120.0, 80.0])
        self.core.set_owner(USER, self.room, "s-a")
        self.assertEqual(self.core.room(USER, self.room)["owner"], "s-a")
        with self.assertRaises(Exception):
            self.core.set_owner("s-b", self.room, "s-b")                    # 成員不能自己當主持
        self.core.register("s-x", "codex", "外人")
        with self.assertRaises(Exception):
            self.core.set_owner(USER, self.room, "s-x")                     # 不在房裡

    def test_resume_clears_the_pause(self):
        self.pause()
        self.assertTrue(self.core.resume("s-boss", self.room)["resumed"])
        self.assertEqual(self.core.post("s-a", self.room, "@b 繼續")["wake"], ["s-b"])

    def test_delivery_records_and_lights_once(self):
        self.core.record_delivery("s-a", "unavailable", "額度用完", light=True)
        self.core.record_delivery("s-a", "unavailable", "額度用完", light=True)
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)
        self.assertEqual(len(self.core.deliveries()), 2)
        agent = next(a for a in self.core.overview()["agents"] if a["session"] == "s-a")
        self.assertEqual(agent["delivery"]["action"], "unavailable")


class LiveServer(unittest.TestCase):
    """真的開一個核心，給 hook 和轉送程式打。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = Core(self._tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.core))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._patches = [mock.patch.object(cli, "BASE", base), mock.patch.object(dispatch, "BASE", base)]
        for p in self._patches:
            p.start()
        self.core.register("s-claude", "claude", "主持")
        self.core.register("s-cx", "codex", "工人")
        self.room = self.core.create_room("s-claude", "房")["room_id"]
        for sid in ("s-claude", "s-cx"):
            self.core.move(sid, sid, self.room)
            self.core.listen(sid, wait=0)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def run_hook(self, session, event, prompt="嗨"):
        out = io.StringIO()
        payload = {"session_id": session, "hook_event_name": event, "prompt": prompt}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out):
            cli.hook()
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else None


class RemoteTest(unittest.TestCase):
    """遠端入口：一律要鑰匙，身分由鑰匙決定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = Core(self._tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.core, remote=True))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        for sid, name in (("s-boss", "主持"), ("s-res", "管家")):
            self.core.register(sid, "claude", name)
        self.room = self.core.create_room("s-boss", "房")["room_id"]
        self.core.move("s-boss", "s-boss", self.room)
        self.core.set_global(USER, "s-res", "resident", "總管")
        self.res_key = self.core.issue_key(USER, "s-res")["key"]
        self.user_key = self.core.issue_key(USER, USER)["key"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def http(self, method, path, key=None, **params):
        import urllib.request, urllib.error
        import urllib.parse
        data = json.dumps(params).encode() if method == "POST" else None
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if method == "GET" and params else "")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {key}"} if key else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_no_key_or_wrong_key_is_refused(self):
        self.assertEqual(self.http("GET", "/api/overview")[0], 401)
        self.assertEqual(self.http("GET", "/api/overview", key="wrong-key")[0], 401)
        self.assertEqual(self.http("GET", "/api/overview", key=self.res_key)[0], 200)

    def test_identity_comes_from_the_key_not_as(self):
        status, got = self.http("POST", f"/api/rooms/{self.room}/post", key=self.res_key, text="大家好", **{"as": USER})
        self.assertEqual(status, 200)
        self.assertEqual(got["result"]["from"], "s-res")                 # 冒充使用者沒用
        status, got = self.http("POST", f"/api/rooms/{self.room}/post", key=self.user_key, text="我來了")
        self.assertEqual(got["result"]["from"], USER)

    def test_resident_manages_but_cannot_delete_or_issue_keys(self):
        self.assertEqual(self.http("POST", "/api/rooms", key=self.res_key, name="新房", owner="s-boss")[0], 200)
        self.assertEqual(self.http("POST", f"/api/rooms/{self.room}/delete", key=self.res_key)[0], 403)
        self.assertEqual(self.http("POST", "/api/keys", key=self.res_key, identity="s-res")[0], 403)
        self.assertEqual(self.http("POST", "/api/keys", key=self.user_key, identity="s-res")[0], 403)   # 只在本機發
        self.assertEqual(self.http("GET", "/api/listen", key=self.res_key, session="s-boss", wait=0)[0], 403)
        self.assertEqual(self.http("GET", "/api/listen", key=self.res_key, session="s-res", wait=0)[0], 200)

    def test_revoked_key_stops_working(self):
        self.core.revoke_keys(USER, "s-res")
        self.assertEqual(self.http("GET", "/api/overview", key=self.res_key)[0], 401)


class HookTest(LiveServer):
    def test_user_prompt_gets_rules_and_backlog(self):
        self.core.post(USER, self.room, "背景")
        context = self.run_hook("s-claude", "UserPromptSubmit")
        self.assertIn("你在房裡叫「主持」", context)
        self.assertIn(f"【房間 房（R1）｜來自 {USER_NAME}】背景", context)
        again = self.run_hook("s-claude", "UserPromptSubmit")
        self.assertNotIn("房間的新訊息", again)                          # 補過的不再補

    def test_stop_hook_waits_and_wakes_on_mention(self):
        self.core.post(USER, self.room, "@工人 只是背景")                 # 沒找主持，不叫醒
        self.core.post("s-cx", self.room, "@主持 有問題")
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit) as ctx:
            cli.rewake("s-claude")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("@主持 有問題", err.getvalue())
        self.assertIsNone(self.run_hook("s-claude", "PostToolUse"))      # 已經送過，不重送

    def test_replaced_waiter_leaves_messages_for_the_new_one(self):
        import tempfile as tf
        mark = os.path.join(tf.gettempdir(), "agent-rooms-wait-s-claude")
        self.core.post("s-cx", self.room, "@主持 有問題")
        real_call = cli.call

        def call(method, path, *a, **k):
            result = real_call(method, path, *a, **k)
            if path == "/api/listen":
                with open(mark, "w") as fh:
                    fh.write("someone-newer")                           # 等的時候有新的等待者接手
            return result
        with mock.patch.object(cli, "call", call):
            self.assertIsNone(cli.rewake("s-claude"))
        left = self.core.listen("s-claude", wait=0, triggers_only=True)["events"]
        self.assertIn("@主持 有問題", [e.get("text") for e in left])        # 舊的沒把訊息吃掉

    def test_user_prompt_resets_hops_but_wakeups_do_not(self):
        self.core.state["rooms"][self.room].update(hops=MAX_HOPS + 1, paused=True)
        self.run_hook("s-claude", "UserPromptSubmit", "<task-notification>\n<summary>Stop hook feedback</summary>")
        self.assertTrue(self.core.state["rooms"][self.room]["paused"])    # 房間叫醒的那一輪不算 使用者
        self.run_hook("s-claude", "UserPromptSubmit", "阿還沒標完喔?")
        self.assertFalse(self.core.state["rooms"][self.room]["paused"])
        self.assertEqual(self.core.state["rooms"][self.room]["hops"], 0)

    def test_user_typing_in_a_codex_chat_resets_hops(self):
        self.core.state["rooms"][self.room].update(hops=MAX_HOPS + 1, paused=True)
        w = dispatch.Worker("s-cx")
        with mock.patch.object(dispatch, "last_seen", return_value=w.user_seen + 100):
            w.check_user()
        self.assertFalse(self.core.state["rooms"][self.room]["paused"])

    def test_maintainer_gets_rules_summary_and_calls(self):
        self.core.register("s-m", "claude", "修房")
        self.core.set_global(USER, "s-m", "maintainer", "維修")
        self.core.post("s-cx", self.room, "@維修 送不到")
        context = self.run_hook("s-m", "UserPromptSubmit")
        self.assertIn(f"你是 {USER_NAME} 房間工具的維修", context)
        self.assertIn("各房間現況", context)
        self.assertIn(f"{OWNER_LABEL} 主持", context)
        self.assertIn("@維修 送不到", context)
        self.assertIsNone(self.run_hook("s-m", "PostToolUse"))           # 沒人再找它就不插話
        self.core.post("s-cx", self.room, "@維修 還是不行")
        self.assertIn("還是不行", self.run_hook("s-m", "PostToolUse"))

    def test_member_rules_name_the_maintainer_only_when_there_is_one(self):
        self.assertNotIn("找維修", self.run_hook("s-claude", "UserPromptSubmit"))
        self.core.register("s-m", "claude", "修房")
        self.core.set_global(USER, "s-m", "maintainer", "工具人")
        self.assertIn("寫「@工具人」找維修", self.run_hook("s-claude", "UserPromptSubmit"))

    def test_resident_gets_its_own_rules(self):
        self.core.register("s-m", "claude", "修房")
        self.core.set_global(USER, "s-m", "maintainer", "工具人")
        self.core.register("s-r", "claude", "管家")
        self.core.set_global(USER, "s-r", "resident")
        context = self.run_hook("s-r", "UserPromptSubmit")
        self.assertIn(f"你是 {USER_NAME} 的常駐代理", context)
        self.assertIn("「@工具人」找維修", context)
        self.assertIn("各房間現況", context)

    def test_post_tool_use_only_interrupts_for_triggers(self):
        self.core.post(USER, self.room, "@工人 只是背景")                 # 找的是別人，對主持是背景
        self.assertIsNone(self.run_hook("s-claude", "PostToolUse"))
        self.core.post("s-cx", self.room, "@主持 請檢查")
        context = self.run_hook("s-claude", "PostToolUse")
        self.assertIn("只是背景", context)
        self.assertIn("@主持 請檢查", context)
        self.assertNotIn("你在房裡叫", context)

    def test_not_in_a_room_or_core_down_is_silent(self):
        self.assertIsNone(self.run_hook("someone-else", "UserPromptSubmit"))
        with mock.patch.object(cli, "BASE", "http://127.0.0.1:9"):
            self.assertIsNone(self.run_hook("s-claude", "UserPromptSubmit"))


class DispatchTest(LiveServer):
    def worker(self):
        w = dispatch.Worker("s-cx")
        return w

    def pending(self):
        return self.core.listen("s-cx", wait=0, triggers_only=True, consume=False)

    def fake_adapter(self, status, send_error=None):
        sent = []

        def adapter(kind, sock, request):
            if request["method"] == "thread/state":
                return {"threadRuntimeStatus": {"type": status}}, None
            sent.append(request)
            return (None, send_error) if send_error else ({"turn": {"id": "t1"}}, None)
        return adapter, sent

    def deliver(self, status, send_error=None, where=("cli", "/sock", "測試帳號")):
        adapter, sent = self.fake_adapter(status, send_error)
        got = self.pending()
        with mock.patch.object(dispatch, "locate", return_value=where), \
                mock.patch.object(dispatch, "adapter", adapter), \
                mock.patch.object(dispatch.time, "sleep"), mock.patch.object(dispatch, "window_open", return_value=False):
            w = self.worker()
            w.headless = got.get("headless")
            w.deliver(got["room_id"], got["events"])
        return sent

    def test_idle_gets_turn_start_and_is_acked(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        sent = self.deliver("idle")
        self.assertEqual(sent[0]["method"], "turn/start")
        self.assertIn("@工人 做 T1", sent[0]["params"]["input"][0]["text"])
        self.assertEqual(self.pending()["events"], [])
        self.assertEqual(self.core.deliveries()[-1]["action"], "start")

    def test_turn_that_fails_right_after_is_reported(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        calls = []

        def adapter(kind, sock, request):
            if request["method"] == "thread/state":
                calls.append(1)
                return {"threadRuntimeStatus": {"type": "idle" if len(calls) == 1 else "systemError"}}, None
            return {"turn": {"id": "t1"}}, None
        got = self.pending()
        with mock.patch.object(dispatch, "locate", return_value=("cli", "/sock", "測試帳號")), \
                mock.patch.object(dispatch, "adapter", adapter), mock.patch.object(dispatch.time, "sleep"):
            self.worker().deliver(got["room_id"], got["events"])
        self.assertEqual([d["action"] for d in self.core.deliveries()][-2:], ["start", "failed_after"])
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)

    def test_closed_window_goes_offline_until_user_returns(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        adapter, sent = self.fake_adapter("notLoaded")
        got = self.pending()
        with mock.patch.object(dispatch, "locate", return_value=("cli", "/sock", "測試帳號")), \
                mock.patch.object(dispatch, "adapter", adapter), mock.patch.object(dispatch.time, "sleep"), \
                mock.patch.object(dispatch, "window_open", return_value=False):
            self.worker().deliver(got["room_id"], got["events"])
        self.assertEqual(sent, [])                                     # 關著就不替他跑一輪
        self.assertTrue(self.core.state["agents"]["s-cx"]["off"])
        self.assertEqual(self.pending()["events"], [])                 # 下線：不再算該叫醒
        self.assertEqual(self.core.room(USER, self.room)["lit"], 0)  # 正常狀況，不亮燈
        notice = self.core.post("s-claude", self.room, "@工人 再一則")["notice"]
        self.assertIn("已下線", notice)                                 # 找他的人知道
        self.core.set_off(USER, "s-cx", False)
        texts = [e.get("text") for e in self.pending()["events"]]
        self.assertEqual(texts[-2:], ["@工人 做 T1", "@工人 再一則"])     # 回到線上：留著的一起補
        self.assertEqual(self.deliver("idle")[0]["method"], "turn/start")

    def test_owner_wakes_closed_window_in_background(self):
        self.core.set_off("s-claude", "s-cx", True, "做完了")
        self.core.post("s-claude", self.room, "@工人 新工作")
        self.assertEqual(self.deliver("notLoaded"), [])                # 下線：還是不叫
        self.core.set_off("s-claude", "s-cx", False)                   # 負責人叫回來
        sent = self.deliver("notLoaded")
        self.assertEqual(sent[0]["method"], "turn/start")              # 沒有視窗也在背景送
        self.assertIn("@工人 新工作", sent[0]["params"]["input"][0]["text"])
        self.assertFalse(self.core.state["agents"]["s-cx"]["off"])
        self.core.set_off("s-claude", "s-cx", True)                    # 再標下線就不再背景跑
        self.assertNotIn("headless", self.core.state["agents"]["s-cx"])

    def test_self_back_online_is_not_headless(self):
        self.core.set_off("s-cx", "s-cx", True, "視窗關了")
        self.core.set_off("s-cx", "s-cx", False)                       # 使用者 回到那個視窗
        self.assertNotIn("headless", self.core.state["agents"]["s-cx"])

    def test_spawned_member_is_headless(self):
        self.core.register("s-new", "codex", "新人", spawned_by="s-claude")
        self.assertTrue(self.core.state["agents"]["s-new"]["headless"]["spawned"])

    def test_user_turn_waits_until_idle(self):
        self.core.post("s-claude", self.room, "@工人 順便看一下")
        with mock.patch.object(dispatch, "user_active", return_value=120):
            self.assertEqual(self.deliver("active"), [])               # 使用者 那一輪還在跑：不插
            self.assertEqual(self.core.deliveries()[-1]["action"], "delayed")
            self.assertEqual(self.deliver("idle")[0]["method"], "turn/start")   # 一閒下來就送，不等計時

    def test_owner_can_mark_done_members_offline(self):
        self.core.set_off("s-claude", "s-cx", True, "做完了")
        with self.assertRaises(Exception):
            self.core.set_off("s-cx", "s-claude", True)                # 成員不能把負責人標下線
        self.core.post("s-claude", self.room, "@工人 還在嗎")
        self.assertEqual(self.pending()["events"], [])

    def test_running_gets_steer(self):
        self.core.post("s-claude", self.room, "@工人 順便看一下")
        self.assertEqual(self.deliver("active")[0]["method"], "turn/steer")

    def test_quota_exhausted_is_recorded_lit_and_kept(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        self.assertEqual(self.deliver("systemError"), [])
        self.assertEqual(len(self.pending()["events"]), 1)            # 訊息留著
        self.assertEqual(self.core.deliveries()[-1]["action"], "unavailable")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)

    def test_race_is_retried_not_acked(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        self.deliver("idle", send_error="Thread is active; use turn/steer. No message sent.")
        self.assertEqual(len(self.pending()["events"]), 1)

    def test_uncertain_is_acked_and_lit_not_resent(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        self.deliver("idle", send_error="Daemon request timed out; 結果不確定，先查紀錄。")
        self.assertEqual(self.pending()["events"], [])
        self.assertEqual(self.core.deliveries()[-1]["action"], "uncertain")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)

    def test_detached_window_is_kept_and_told_once(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        err = "thread s-cx already has an active writer"
        worker = self.worker()
        adapter, _ = self.fake_adapter("notLoaded", err)
        with mock.patch.object(dispatch, "locate", return_value=("cli", "/sock", "測試帳號")), \
                mock.patch.object(dispatch, "adapter", adapter), mock.patch.object(dispatch.time, "sleep"), \
                mock.patch.object(dispatch, "window_open", return_value=True):
            for _ in range(2):
                got = self.pending()
                worker.deliver(got["room_id"], got["events"])
        self.assertEqual(len(self.pending()["events"]), 1)            # 沒收下就不 ack
        notices = [e for e in self.core.history(USER, self.room) if e["type"] == "delivery"]
        self.assertEqual(len(notices), 1)                              # 重試不洗版
        self.assertEqual(notices[0]["wake"], ["s-claude"])             # 叫醒找他的人
        self.assertIn("codex resume s-cx", notices[0]["detail"])

    def test_unknown_thread_is_reported(self):
        self.core.post("s-claude", self.room, "@工人 做 T1")
        with mock.patch.object(dispatch, "locate", return_value=None), mock.patch.object(dispatch.time, "sleep"):
            got = self.pending()
            self.worker().deliver(got["room_id"], got["events"])
        self.assertEqual(self.core.deliveries()[-1]["action"], "not_found")

    def test_user_just_spoke_delays(self):
        with tempfile.TemporaryDirectory() as d:
            import datetime
            with open(f"{d}/s-cx.json", "w") as fh:
                json.dump({"last_seen": datetime.datetime.now().isoformat(timespec="seconds")}, fh)
            with mock.patch.object(dispatch, "ACC_SESSIONS", d):
                self.assertGreater(dispatch.user_active("s-cx", 0), 100)
                self.assertEqual(dispatch.user_active("s-cx", __import__("time").time()), 0)  # 那是我們送的


if __name__ == "__main__":
    unittest.main()
