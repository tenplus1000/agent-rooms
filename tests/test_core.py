import os
os.environ["ROOMS_JUDGE"] = "off"          # 測試不打真的判官；判官另外 mock
import tempfile
import threading
import time
import unittest

from rooms.core import USER, USER_NAME, Core, RoomError


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

    def other_room(self):
        """第二間房：codex-1 當負責人。"""
        rid = self.core.create_room(USER, "標資料", owner="s-cx1")["room_id"]
        self.core.move("s-cx1", "s-cx1", rid)
        self.core.move(USER, "s-cx2", rid)
        return rid

    def test_host_messages_another_host_by_name_or_room_id(self):
        other = self.other_room()
        got = self.core.post("s-claude", self.room, "@codex-1 你那邊的標註格式給我")
        self.assertEqual(got["relayed"], [f"codex-1（{other}）"])
        rows = self.drain("s-cx1")
        self.assertTrue(any("〔從 素材包" in e["text"] and "s-cx1" in e["wake"] for e in rows if e["type"] == "post"))
        self.assertFalse(any("s-cx2" in e.get("wake", ()) for e in self.drain("s-cx2")))   # 對方房的成員沒被叫醒
        self.core.post("s-cx1", other, f"@{self.room} 格式在白板")                 # 直接寫房號也行
        self.assertTrue(any("〔從 標資料" in e.get("text", "") for e in self.drain("s-claude")))

    def test_relay_asks_the_judge_which_rooms_and_logs_the_named_ones(self):
        # 先只記不用：點名的那幾房是答案，判官看不到 @ 了誰
        from unittest import mock
        import json
        from pathlib import Path
        other = self.other_room()
        third = self.core.create_room(USER, "台文", owner="s-cx2")["room_id"]
        self.core.move(USER, "s-cx2", third)
        asked = threading.Event()
        def fake(log_path, state, rooms, meta):
            fake.args = (state, rooms, meta)
            asked.set()
            return None, "skip"
        with mock.patch.dict(os.environ, {"ROOMS_JUDGE": "on", "ROOMS_JUDGE_KEY": "k"}), \
                mock.patch("rooms.judge.relay_rooms", side_effect=fake):
            got = self.core.post("s-claude", self.room, "@codex-1 你們判官的題目怎麼存")
        self.assertTrue(asked.wait(2))
        state, rooms, meta = fake.args
        self.assertEqual(sorted(r for r, _ in rooms), sorted([other, third]))
        self.assertEqual((meta["room_id"], meta["seq"], meta["chosen"]), (self.room, got["seq"], [other]))
        self.assertIn("判官的題目怎麼存", state)
        self.assertNotIn("@codex-1", state)

    def test_host_of_several_rooms_gets_it_where_they_last_spoke(self):
        # 台文主持開了幾間對唱房、自己人不在任何一間：別房用名字找他，要送到他最近講話的主房，不是最後開的那間
        main = self.other_room()
        self.core.post("s-cx1", main, f"@{self.room} 想問你們判官的事")
        side = self.core.create_room(USER, "對唱一", owner="s-cx1")["room_id"]
        self.core.move("s-cx1", "s-cx1", side)
        self.core.move("s-cx1", "s-cx1", None)
        got = self.core.post("s-claude", self.room, "@codex-1 回你三題")
        self.assertEqual(got["relayed"], [f"codex-1（{main}）"])
        self.core.move("s-cx1", "s-cx1", side)                                    # 人坐在哪間就先送那間
        got = self.core.post("s-claude", self.room, "@codex-1 再補一題")
        self.assertEqual(got["relayed"], [f"codex-1（{side}）"])

    def test_tools_are_registered_by_the_user_and_linked_to_rooms(self):
        # 本機工具：使用者登記、接到房間；房裡的人看得到接上的，看不到別間的
        with self.assertRaises(RoomError):
            self.core.set_tool("s-cx1", {"name": "x", "category": "dashboard", "url": "http://127.0.0.1:1/"})
        with self.assertRaises(RoomError):
            self.core.set_tool(USER, {"name": "x", "category": "別的", "url": "http://127.0.0.1:1/"})
        kit = self.core.set_tool(USER, {"name": "儀表板", "category": "dashboard", "url": "http://127.0.0.1:8790/"})
        other = self.other_room()
        self.core.link_tool(USER, kit["id"], self.room)
        self.assertEqual([t["name"] for t in self.core.room_tools("s-claude", self.room)], ["儀表板"])
        self.assertEqual(self.core.room_tools("s-cx1", other), [])
        self.assertTrue(any(e["type"] == "tool_linked" and not e.get("wake") for e in self.core.history(USER, self.room)))
        moved = self.core.set_tool(USER, {"id": kit["id"], "pos": [10, 20]})
        self.assertEqual((moved["pos"], moved["rooms"]), ([10.0, 20.0], [self.room]))
        self.core.delete_tool(USER, kit["id"])
        self.assertEqual(self.core.room_tools("s-claude", self.room), [])

    def test_posts_carry_files_only_people_in_the_room_can_read(self):
        import base64
        got = self.core.save_file("s-claude", self.room, "截圖.png", base64.b64encode(b"\x89PNG fake").decode())
        self.assertTrue(got["rel"].startswith(self.room + "/") and got["type"] == "image/png")
        event = self.core.post("s-claude", self.room, "", files=[got])          # 只有附件、沒有字也可以
        self.assertEqual(event["files"][0]["name"], "截圖.png")
        self.assertEqual(self.core.read_file(USER, got["rel"])[0], b"\x89PNG fake")
        other = self.other_room()
        with self.assertRaises(RoomError):
            self.core.read_file("s-cx1", got["rel"])                            # 別間房的人拿不到
        with self.assertRaises(RoomError):
            self.core.post("s-cx1", other, "偷用", files=[got])                  # 也不能拿去別間房附
        with self.assertRaises(RoomError):
            self.core.read_file(USER, f"{self.room}/../../state.json")

    def test_rewind_only_moves_back_and_only_for_admins(self):
        self.core.ack("s-claude", self.room, 9)
        with self.assertRaises(RoomError):
            self.core.rewind("s-cx1", "s-claude", self.room, 2)
        self.assertEqual(self.core.rewind(USER, "s-claude", self.room, 2), 2)
        self.assertEqual(self.core.rewind(USER, "s-claude", self.room, 5), 2)       # 不會往前推

    def test_only_hosts_relay(self):
        other = self.other_room()
        self.core.post("s-cx2", other, "@主持 我想直接問你")                      # 成員不是負責人：不跨房
        self.assertFalse(any("〔從" in e.get("text", "") for e in self.core.history(USER, self.room)))

    def test_adjacent_rooms_share_boards_read_only(self):
        other = self.other_room()
        self.core.set_whiteboard("s-claude", self.room, "素材包的規矩")
        self.core.set_whiteboard("s-cx1", other, "標資料的規矩")
        self.core.set_room_pos(USER, self.room, 100, 100)
        self.core.set_room_pos(USER, other, 400, 400)
        self.assertEqual(self.core.neighbor_boards("s-claude", self.room), [])
        self.core.set_room_pos(USER, other, 100 + 150 * 0.866, 100 + 75)           # 貼在右後面
        boards = self.core.neighbor_boards("s-claude", self.room)
        self.assertEqual([(b["room_id"], b["whiteboard"]) for b in boards], [(other, "標資料的規矩")])
        self.assertTrue(any(e["type"] == "linked" for e in self.core.history(USER, other)))
        with self.assertRaises(RoomError):
            self.core.set_whiteboard("s-claude", other, "改別人的")              # 看得到，改不了
        self.core.set_whiteboard("s-cx1", other, "新規矩")
        self.assertTrue(any(e["type"] == "whiteboard" and e.get("neighbor") == other
                            for e in self.core.history(USER, self.room)))
        self.core.set_room_pos(USER, other, 100, 100 - 100)                         # 疊在上面也算
        self.assertEqual(self.core.overview()["rooms"][0]["neighbors"], {other: "上"})
        self.core.set_room_pos(USER, other, 600, 600)                               # 拉開就不共用
        self.assertEqual(self.core.neighbor_boards("s-claude", self.room), [])
        self.assertTrue(any(e["type"] == "unlinked" for e in self.core.history(USER, self.room)))

    def test_only_user_deletes_an_empty_room_and_keeps_its_log(self):
        with self.assertRaises(RoomError):
            self.core.delete_room("s-claude", self.room)                   # 負責人不能刪
        with self.assertRaises(RoomError):
            self.core.delete_room(USER, self.room)                          # 還有人在
        for sid in list(self.core._members(self.room)):
            self.core.move(sid, sid, None)
        self.core.delete_room(USER, self.room)
        self.assertEqual(self.core.list_rooms(USER), [])
        self.assertEqual(len(list((self.core.data / "rooms" / "deleted").glob("*.jsonl"))), 1)

    def test_delete_with_evict_only_when_everyone_is_off(self):
        self.core.move(USER, "s-cx1", self.room)
        self.core.set_off(USER, "s-claude", True)
        with self.assertRaises(RoomError):
            self.core.delete_room(USER, self.room, evict=True)              # codex-1 還在線上
        self.core.set_off(USER, "s-cx1", True)
        self.core.delete_room(USER, self.room, evict=True)
        self.assertEqual(self.core.list_rooms(USER), [])
        self.assertIsNone(self.core.state["agents"]["s-cx1"]["room_id"])

    def test_shelve_offline_agents_and_unshelve_on_return(self):
        with self.assertRaises(RoomError):
            self.core.shelve(USER, "s-cx2", True)                           # 還沒下線
        self.core.set_off(USER, "s-cx2", True)
        with self.assertRaises(RoomError):
            self.core.shelve("s-claude", "s-cx2", True)                     # 只有使用者或維修
        self.core.shelve(USER, "s-cx2", True)
        row = next(a for a in self.core.overview()["agents"] if a["session"] == "s-cx2")
        self.assertTrue(row["shelved"])
        self.core.set_off(USER, "s-cx2", False)                             # 叫回來就拿出來
        row = next(a for a in self.core.overview()["agents"] if a["session"] == "s-cx2")
        self.assertFalse(row["shelved"])
        self.core.set_off(USER, "s-claude", True)
        with self.assertRaises(RoomError):
            self.core.shelve(USER, "s-claude", True)                        # 還在房裡

    def test_owner_or_user_renames_room(self):
        self.assertEqual(self.core.rename_room("s-claude", self.room, " 桌遊社 ")["name"], "桌遊社")
        self.assertEqual(self.core.rename_room(USER, self.room, "素材包")["name"], "素材包")
        with self.assertRaises(RoomError):
            self.core.rename_room("s-cx1", self.room, "搶房")
        with self.assertRaises(RoomError):
            self.core.rename_room("s-claude", self.room, " ")

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
        self.core.post("s-claude", self.room, f"@{USER} 這裡要你決定")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 1)
        thread = self.core.threads(USER, self.room)[0]
        self.assertTrue(thread["needs_user"])
        self.core.post(USER, self.room, "照 A 做")
        self.assertEqual(self.core.room(USER, self.room)["lit"], 0)

    def test_every_event_says_who_said_it(self):
        self.core.post(USER, self.room, "嗨")
        event = self.core.history("s-claude", self.room)[-1]
        self.assertEqual((event["from"], event["from_name"], event["room_id"]), (USER, USER_NAME, self.room))

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
