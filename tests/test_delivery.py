import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

from rooms import cli, dispatch
from rooms.core import MAX_HOPS, USER, Core
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
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)
        self.core.post(USER, self.room, "好了，b 繼續")
        self.assertEqual(self.core.post("s-a", self.room, "@b 繼續")["wake"], ["s-b"])
        self.assertEqual(self.core.room(USER, self.room)["lit"], 0)

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

    def run_hook(self, session, event):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": session, "hook_event_name": event}))), \
                redirect_stdout(out):
            cli.hook()
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else None


class HookTest(LiveServer):
    def test_user_prompt_gets_rules_listen_hint_and_backlog(self):
        self.core.state["agents"]["s-claude"]["listening_at"] = 0       # 沒有 Monitor 在聽
        self.core.post(USER, self.room, "背景")
        context = self.run_hook("s-claude", "UserPromptSubmit")
        self.assertIn("你在房裡叫「主持」", context)
        self.assertIn("listen --session s-claude", context)
        self.assertIn("【房間 房（R1）｜來自 使用者】背景", context)
        again = self.run_hook("s-claude", "UserPromptSubmit")
        self.assertNotIn("房間的新訊息", again)                          # 補過的不再補
        self.assertIn("listen --session s-claude", again)               # hook 本身不算心跳

    def test_post_tool_use_only_interrupts_for_triggers(self):
        self.core.post("s-cx", self.room, "只是背景")
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
                mock.patch.object(dispatch.time, "sleep"):
            self.worker().deliver(got["room_id"], got["events"])
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
