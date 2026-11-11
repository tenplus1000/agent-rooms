import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from rooms import dispatch


class ReopenTest(unittest.TestCase):
    """Claude 視窗關了才重開：認得出對話開著沒、從紀錄找回工作資料夾與權限模式。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.saved = dispatch.CLAUDE_HOME
        dispatch.CLAUDE_HOME = str(self.home)
        (self.home / "sessions").mkdir()
        (self.home / "projects" / "-work").mkdir(parents=True)

    def tearDown(self):
        dispatch.CLAUDE_HOME = self.saved
        self._tmp.cleanup()

    def test_alive_only_when_pid_and_start_time_match(self):
        with open(f"/proc/{os.getpid()}/stat") as fh:
            started = fh.read().rsplit(")", 1)[1].split()[19]
        row = {"pid": os.getpid(), "sessionId": "s-1", "procStart": started}
        (self.home / "sessions" / "1.json").write_text(json.dumps(row))
        self.assertTrue(dispatch.claude_alive("s-1"))
        self.assertFalse(dispatch.claude_alive("s-2"))
        row["procStart"] = "1"                                  # pid 被別的程式重用了
        (self.home / "sessions" / "1.json").write_text(json.dumps(row))
        self.assertFalse(dispatch.claude_alive("s-1"))

    def test_open_window_without_a_waiting_hook_is_noticed(self):
        # hook 等到 timeout 會被結束：視窗開著也叫不醒，要看得出來
        (self.home / "sessions" / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "s-1", "status": "idle"}))
        self.assertTrue(dispatch.claude_idle("s-1"))
        self.assertIsNone(dispatch.claude_idle("s-9"))
        with mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
            mark = self.home / "agent-rooms-wait-s-1"
            self.assertFalse(dispatch.hook_waiting("s-1"))                   # 沒有等待者
            mark.write_text(str(os.getpid()))                                # 這支測試的 pid：命令列有 room（unittest 跑 tests/）？
            with open(f"/proc/{os.getpid()}/cmdline", "rb") as fh:
                self.assertEqual(dispatch.hook_waiting("s-1"), b"room" in fh.read())
            mark.write_text("999999999")                                     # 已經結束的
            self.assertFalse(dispatch.hook_waiting("s-1"))

    def test_only_fresh_calls_open_a_window(self):
        import datetime
        now = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc).timestamp()
        at = lambda ago: datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc).isoformat()
        events = [{"seq": 1, "type": "join", "at": at(10), "wake": ["s-1"]},          # 進房、換主持：不算找他
                  {"seq": 2, "type": "owner", "at": at(10), "wake": ["s-1"]},
                  {"seq": 3, "type": "post", "at": at(3600), "wake": ["s-1"]},        # 一小時前的積壓
                  {"seq": 4, "type": "post", "at": at(10), "wake": ["s-2"]}]          # 找別人
        self.assertEqual(dispatch.calls_for("s-1", events, now), [])
        events.append({"seq": 5, "type": "post", "at": at(30), "wake": ["s-1"]})
        self.assertEqual([e["seq"] for e in dispatch.calls_for("s-1", events, now)], [5])

    def test_self_call_stays_fresh_while_it_is_retried(self):
        # @自己 沒叫醒會每 5 分鐘再試，總共 3 次：12 分鐘前的還算數，別人的同樣舊就不算
        import datetime
        now = datetime.datetime(2026, 10, 28, tzinfo=datetime.timezone.utc).timestamp()
        at = datetime.datetime.fromtimestamp(now - 720, datetime.timezone.utc).isoformat()
        events = [{"seq": 1, "type": "post", "at": at, "wake": ["s-1"], "from": "s-1", "self_call": True},
                  {"seq": 2, "type": "post", "at": at, "wake": ["s-1"], "from": "s-2"}]
        self.assertEqual([e["seq"] for e in dispatch.calls_for("s-1", events, now)], [1])

    def test_same_batch_goes_by_time_not_room_seq(self):
        # 編號是各房間自己數的：上次在別間房第 685 則開過，這間房第 60 則的新訊息還是要開
        import datetime
        opened = datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc).timestamp()
        at = lambda later: datetime.datetime.fromtimestamp(opened + later, datetime.timezone.utc).isoformat()
        self.assertFalse(dispatch.same_batch([{"seq": 60, "at": at(86400)}], opened))
        self.assertTrue(dispatch.same_batch([{"seq": 700, "at": at(-30)}], opened))   # 開視窗前就在的那批
        self.assertFalse(dispatch.same_batch([{"seq": 1, "at": at(-30)}], 0))          # 從沒開過

    def test_context_used_reads_codex_and_claude_records(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "2026" / "10" / "28").mkdir(parents=True)
        (d / "2026" / "10" / "28" / "rollout-x-th-1.jsonl").write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": 180000}, "model_context_window": 240000}}}) + "\n")
        with mock.patch.object(dispatch, "WSL_SESSIONS", str(d)):
            self.assertAlmostEqual(dispatch.context_used("th-1", "codex"), 75.0)
        rows = [{"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 10, "cache_read_input_tokens": 140000}}},
                {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}}]
        (self.home / "projects" / "-work" / "s-5.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        self.assertAlmostEqual(dispatch.context_used("s-5", "claude"), 70.005)   # 額度用完的假回應不算

    def test_transcript_gives_cwd_and_last_permission_mode(self):
        rows = [{"type": "permission-mode", "permissionMode": "default"}, {"type": "user", "cwd": "/srv/a"},
                {"type": "permission-mode", "permissionMode": "auto"}, {"type": "assistant", "cwd": "/srv/b"}]
        (self.home / "projects" / "-work" / "s-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        # 開始時的資料夾（/srv/a）：重開時要用它，記憶和專案指示才會照原本的載入；中途 cd 到 /srv/b 不算
        self.assertEqual(dispatch.claude_transcript("s-1")[1:3], ("/srv/a", "auto"))
        self.assertIsNone(dispatch.claude_transcript("s-9"))

    def test_window_on_another_account_reopens_on_that_account(self):
        # 額度用完換帳號開的視窗：紀錄在第二個帳號底下，接回時也要用那個帳號
        import tempfile
        other = Path(tempfile.mkdtemp())
        (other / "projects" / "-work").mkdir(parents=True)
        (other / "projects" / "-work" / "s-2.jsonl").write_text(json.dumps({"type": "user", "cwd": "/srv/c"}))
        state = Path(tempfile.mkdtemp())
        with mock.patch.dict(dispatch.CLAUDE_NAMES, {"claude2": str(other)}), \
                mock.patch.dict(os.environ, {"HOME": str(state)}), \
                mock.patch.object(dispatch, "CLAUDE_OPEN", "true"), mock.patch("subprocess.Popen"):
            found = dispatch.claude_transcript("s-2")
            self.assertEqual(found[3], str(other))
            dispatch.open_claude("s-2", "x", found[1], found[2], found[3])
            script = (state / ".local/state/agent-rooms/reopen-s-2.sh").read_text()
        self.assertIn(f"export CLAUDE_CONFIG_DIR=\"{other}\"", script)

    def test_quota_reset_reads_weekly_dates(self):
        import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        now = datetime.datetime(2026, 11, 12, 2, 0, tzinfo=tz).timestamp()
        at = dispatch.quota_reset_at("You've hit your 5-hour limit · resets 3:30am (Asia/Taipei)", now)
        self.assertEqual(datetime.datetime.fromtimestamp(at - 60, tz).strftime("%m-%d %H:%M"), "11-12 03:30")
        at = dispatch.quota_reset_at("You've hit your weekly limit · resets Nov 18, 1am (Asia/Taipei)", now)
        self.assertEqual(datetime.datetime.fromtimestamp(at - 60, tz).strftime("%m-%d %H:%M"), "11-18 01:00")
        self.assertIsNone(dispatch.quota_reset_at("You've hit your limit", now))
        self.assertAlmostEqual(dispatch.quota_reset("看不懂", now), now + 900)

    def _transcript(self, rows):
        (self.home / "projects" / "-work" / "s-q.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

    def test_quota_stuck_only_when_last_reply_is_the_limit_notice(self):
        hit = "2026-11-11T19:15:58.000Z"
        limit = {"type": "assistant", "timestamp": hit, "message": {"model": "<synthetic>", "content": [
            {"type": "text", "text": "You've hit your 5-hour limit · resets 5:50am (Asia/Taipei)"}]}}
        user = {"type": "user", "cwd": "/w", "message": {"content": "做吧"}}
        self._transcript([user, limit, {"type": "system"}])
        got = dispatch.quota_stuck("s-q")
        self.assertIsNotNone(got)
        self.assertGreater(got[1], got[0])
        self._transcript([user, limit, user])                     # 撞到之後又有人講話：那一輪在跑，不算卡著
        self.assertIsNone(dispatch.quota_stuck("s-q"))
        normal = {"type": "assistant", "timestamp": hit, "message": {"model": "claude-x", "content": [{"type": "text", "text": "好"}]}}
        self._transcript([user, limit, user, normal])
        self.assertIsNone(dispatch.quota_stuck("s-q"))

    def test_quota_wake_posts_once_after_reset(self):
        import tempfile
        state = Path(tempfile.mkdtemp())
        posts = []

        def fake_call(method, path, **kw):
            if path == "/api/overview":
                return {"agents": [{"session": "s-q", "name": "主持", "room_id": "R9", "off": None}]}
            posts.append((path, kw))
            return {}
        w = dispatch.ClaudeWorker.__new__(dispatch.ClaudeWorker)
        w.session = "s-q"
        with mock.patch.object(dispatch, "quota_stuck", return_value=(1000.0, 2000.0)), \
                mock.patch.object(dispatch, "call", side_effect=fake_call), \
                mock.patch.dict(os.environ, {"HOME": str(state)}), mock.patch.object(dispatch, "log"):
            self.assertFalse(w.quota_wake(now=1500.0))           # 還沒重置
            self.assertTrue(w.quota_wake(now=2100.0))
            self.assertFalse(w.quota_wake(now=2200.0))           # 同一次撞到只叫一次
        room_posts = [p for p in posts if p[0] == "/api/rooms/R9/post"]
        self.assertEqual(len(room_posts), 1)
        self.assertTrue(room_posts[0][1]["text"].startswith("@主持"))
        self.assertEqual(room_posts[0][1]["as"], "s-q")


if __name__ == "__main__":
    unittest.main()
