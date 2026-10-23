import tempfile
import threading
import time
import unittest

from rooms.core import USER, Core, RoomError


class CoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = Core(self._tmp.name)
        self.core.register("s-claude", "claude", "主持")
        self.core.register("s-cx1", "codex", "codex-1")
        self.core.register("s-cx2", "codex", "codex-2")
        self.room = self.core.create_room("s-claude", "素材包")["room_id"]
        self.core.move("s-claude", "s-claude", self.room)

    def tearDown(self):
        self._tmp.cleanup()

    def drain(self, session):
        return self.core.listen(session, wait=0)["events"]

    # ── 房間、移動、知識邊界 ─────────────────────────────

    def test_one_room_at_a_time_and_moves_are_recorded(self):
        other = self.core.create_room(USER, "讀文件")["room_id"]
        self.core.move(USER, "s-cx1", self.room)
        self.core.move("s-cx1", "s-cx1", other)
        self.assertEqual(self.core.state["agents"]["s-cx1"]["room_id"], other)
        moves = self.core.moves("s-cx1", "s-cx1")
        self.assertEqual([(m["from"], m["to"]) for m in moves], [(None, self.room), (self.room, other)])
        self.assertEqual(moves[0]["by"], USER)
        with self.assertRaises(RoomError):
            self.core.moves("s-cx2", "s-cx1")

    def test_agent_cannot_see_other_rooms(self):
        other = self.core.create_room(USER, "別的房")["room_id"]
        self.core.post(USER, other, "這裡的事")
        with self.assertRaises(RoomError) as ctx:
            self.core.history("s-claude", other)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual([r["room_id"] for r in self.core.list_rooms("s-claude")], [self.room])
        self.assertEqual(len(self.core.list_rooms(USER)), 2)

    def test_after_leaving_the_old_room_is_unreadable(self):
        self.core.move("s-cx1", "s-cx1", self.room)
        self.core.move("s-cx1", "s-cx1", None)
        with self.assertRaises(RoomError):
            self.core.history("s-cx1", self.room)

    def test_latecomer_sees_everything_from_room_creation(self):
        self.core.post("s-claude", self.room, "第一則")
        self.core.set_whiteboard("s-claude", self.room, "規格")
        self.core.move("s-cx1", "s-cx1", self.room)
        texts = [e.get("text") for e in self.drain("s-cx1") if e["type"] == "post"]
        self.assertEqual(texts, ["第一則"])
        self.assertEqual(self.core.history("s-cx1", self.room)[0]["type"], "room_created")

    # ── 訊息與線頭 ─────────────────────────────────────

    def test_mention_opens_thread_and_their_post_closes_it(self):
        self.core.move("s-cx1", "s-cx1", self.room)
        event = self.core.post("s-claude", self.room, "@codex-1 看白板")
        self.assertEqual(event["mentions"], ["s-cx1"])
        open_threads = self.core.threads("s-claude", self.room)
        self.assertEqual([(t["whose"], t["kind"]) for t in open_threads], [("s-cx1", "mention")])
        self.core.post("s-cx1", self.room, "好，看了")
        self.assertEqual(self.core.threads("s-claude", self.room), [])

    def test_task_action_or_leaving_answers_the_mention(self):
        self.core.move("s-cx1", "s-cx1", self.room)
        self.core.move("s-cx2", "s-cx2", self.room)
        task = self.core.create_task("s-claude", self.room, "我要做 A", "做完")["id"]
        self.core.post("s-claude", self.room, "@codex-1 @codex-2 誰來領")
        self.core.claim("s-cx1", task)
        self.core.move("s-cx2", "s-cx2", None)
        self.assertEqual([t for t in self.core.threads("s-claude", self.room) if t["kind"] == "mention"], [])

    def test_repeat_mentions_keep_one_thread(self):
        self.core.move("s-cx1", "s-cx1", self.room)
        self.core.post("s-claude", self.room, "@codex-1 第一次")
        self.core.post("s-claude", self.room, "@codex-1 第二次")
        threads = self.core.threads("s-claude", self.room)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["text"], "@codex-1 第二次")

    def test_mentioning_someone_outside_the_room_does_nothing(self):
        self.core.post("s-claude", self.room, "@codex-2 你在嗎")
        self.assertEqual(self.core.threads("s-claude", self.room), [])

    def test_mentioning_user_lights_the_room(self):
        self.core.post("s-claude", self.room, "@user 這裡要你決定")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)
        thread = self.core.threads(USER, self.room)[0]
        self.assertTrue(thread["needs_user"])
        self.core.post(USER, self.room, "照 A 做")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 0)

    def test_every_event_says_who_said_it(self):
        self.core.post(USER, self.room, "嗨")
        event = self.core.history("s-claude", self.room)[-1]
        self.assertEqual((event["from"], event["from_name"], event["room_id"]), (USER, "使用者", self.room))

    # ── 監聽與勿擾 ─────────────────────────────────────

    def test_listen_skips_own_messages_and_advances(self):
        self.drain("s-claude")
        self.core.post("s-claude", self.room, "自己講的")
        self.assertEqual(self.drain("s-claude"), [])
        self.core.post(USER, self.room, "使用者 講的")
        self.assertEqual([e["text"] for e in self.drain("s-claude")], ["使用者 講的"])
        self.assertEqual(self.drain("s-claude"), [])

    def test_dnd_holds_messages_then_delivers_backlog(self):
        self.drain("s-claude")
        self.core.set_dnd(USER, "s-claude", True)
        self.core.post(USER, self.room, "一")
        self.core.post(USER, self.room, "二")
        got = self.core.listen("s-claude", wait=0)
        self.assertEqual((got["events"], got["dnd"]), ([], True))
        self.core.set_dnd(USER, "s-claude", False)
        self.assertEqual([e["text"] for e in self.drain("s-claude")], ["一", "二"])

    def test_waiting_listener_wakes_on_post(self):
        self.drain("s-claude")
        got = {}

        def wait():
            got.update(self.core.listen("s-claude", wait=5))

        worker = threading.Thread(target=wait)
        worker.start()
        time.sleep(0.2)
        started = time.time()
        self.core.post(USER, self.room, "醒醒")
        worker.join(3)
        self.assertLess(time.time() - started, 2)
        self.assertEqual([e["text"] for e in got["events"]], ["醒醒"])

    def test_listening_is_presence(self):
        self.assertFalse(self.core.room(USER, self.room)["members"][0]["online"])
        self.drain("s-claude")
        self.assertTrue(self.core.room(USER, self.room)["members"][0]["online"])

    def test_state_survives_restart(self):
        self.core.post(USER, self.room, "存起來")
        again = Core(self._tmp.name)
        self.assertEqual(again.history(USER, self.room)[-1]["text"], "存起來")
        self.assertEqual(again.state["agents"]["s-claude"]["room_id"], self.room)

    # ── 白板與任務 ─────────────────────────────────────

    def test_only_owner_and_user_edit_whiteboard_and_create_tasks(self):
        self.core.move("s-cx1", "s-cx1", self.room)
        with self.assertRaises(RoomError):
            self.core.set_whiteboard("s-cx1", self.room, "亂改")
        with self.assertRaises(RoomError):
            self.core.create_task("s-cx1", self.room, "我要做", "做完")
        self.core.set_whiteboard(USER, self.room, "使用者 改的")
        self.assertEqual(self.core.whiteboard("s-cx1", self.room), "使用者 改的")

    def make_task(self):
        for s in ("s-cx1", "s-cx2"):
            self.core.move(s, s, self.room)
        return self.core.create_task("s-claude", self.room, "我要實作第 1 份規格", "測試全過")["id"]

    def test_task_flow_follows_acc_but_verifier_closes(self):
        task = self.make_task()
        self.assertEqual(self.core.tasks("s-cx1", self.room)[0]["verifier"], "s-claude")
        self.core.claim("s-cx1", task)
        with self.assertRaises(RoomError):
            self.core.claim("s-cx2", task)          # 只有一個人領得到
        with self.assertRaises(RoomError):
            self.core.submit("s-cx2", task, "不是我領的")
        self.core.submit("s-cx1", task, "交了 a.py，跑過測試")
        review = [t for t in self.core.threads("s-claude", self.room) if t["kind"] == "review"]
        self.assertEqual(review[0]["whose"], "s-claude")
        self.assertIn("已交、等待檢查", review[0]["text"])
        with self.assertRaises(RoomError):
            self.core.close_task("s-cx1", task, "achieved", "我自己說好")
        done = self.core.close_task("s-claude", task, "achieved", "逐條核對過")
        self.assertEqual((done["status"], done["closed_by"]), ("achieved", "s-claude"))
        self.assertEqual([t for t in self.core.threads("s-claude", self.room) if t["kind"] == "review"], [])
        actions = [h["action"] for h in self.core.state["tasks"][task]["history"]]
        self.assertEqual(actions, ["create", "claim", "submit", "achieved"])

    def test_park_returns_task_to_the_pool(self):
        task = self.make_task()
        self.core.claim("s-cx1", task)
        self.core.park_task("s-cx1", task, "先改別的")
        self.core.claim("s-cx2", task)
        self.assertEqual(self.core.state["tasks"][task]["holder"], "s-cx2")

    def test_revise_keeps_the_old_version(self):
        task = self.make_task()
        self.core.revise_task("s-claude", task, "我要實作第 1 份規格（含匯出）", "", "使用者 補了匯出")
        last = self.core.state["tasks"][task]["history"][-1]
        self.assertEqual(last["before"]["objective"], "我要實作第 1 份規格")
        self.assertEqual(self.core.state["tasks"][task]["criteria"], "測試全過")

    def test_failed_then_amend(self):
        task = self.make_task()
        self.core.claim("s-cx1", task)
        self.core.submit("s-cx1", task, "交了")
        self.core.close_task("s-claude", task, "failed", "少了匯出")
        self.core.amend_task(USER, task, "achieved", "匯出其實不在範圍內")
        self.assertEqual(self.core.state["tasks"][task]["status"], "achieved")

    def test_drop_only_by_owner(self):
        task = self.make_task()
        with self.assertRaises(RoomError):
            self.core.drop_task("s-cx1", task, "不想做")
        self.core.drop_task("s-claude", task, "不需要了")
        self.assertEqual(self.core.state["tasks"][task]["status"], "dropped")

    def test_task_events_reach_listeners(self):
        task = self.make_task()
        self.drain("s-cx1")
        self.core.claim("s-cx2", task)
        events = self.drain("s-cx1")
        self.assertEqual([(e["type"], e["action"]) for e in events if e["type"] == "task"], [("task", "claim")])


if __name__ == "__main__":
    unittest.main()
