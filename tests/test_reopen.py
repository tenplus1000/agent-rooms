import json
import os
import tempfile
import unittest
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

    def test_transcript_gives_cwd_and_last_permission_mode(self):
        rows = [{"type": "permission-mode", "permissionMode": "default"}, {"type": "user", "cwd": "/srv/a"},
                {"type": "permission-mode", "permissionMode": "auto"}, {"type": "assistant", "cwd": "/srv/b"}]
        (self.home / "projects" / "-work" / "s-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        self.assertEqual(dispatch.claude_transcript("s-1")[1:], ("/srv/b", "auto"))
        self.assertIsNone(dispatch.claude_transcript("s-9"))


if __name__ == "__main__":
    unittest.main()
