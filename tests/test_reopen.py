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


if __name__ == "__main__":
    unittest.main()
