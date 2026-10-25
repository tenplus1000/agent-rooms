import json
import os
import tempfile
import unittest
from pathlib import Path

from rooms import dispatch


class ClaudeMemberTest(unittest.TestCase):
    """房間開的 Claude 成員：每一批跑一次 claude -p，第一次開對話、之後接著同一個對話，帶上帳號、模型、推理強度。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.work = tmp / "work"; self.work.mkdir()
        self.record = tmp / "calls.jsonl"
        fake = tmp / "claude"
        fake.write_text(f"""#!/usr/bin/env python3
import json, os, sys
with open({str(self.record)!r}, "a") as fh:
    fh.write(json.dumps({{"args": sys.argv[1:], "cwd": os.getcwd(), "home": os.environ.get("CLAUDE_CONFIG_DIR"),
                         "dispatched": os.environ.get("ROOMS_DISPATCHED")}}) + "\\n")
fail = os.environ.get("FAKE_FAIL")
print("一行警告", file=sys.stderr)
print(json.dumps({{"is_error": bool(fail), "result": "壞了" if fail else "好", "duration_ms": 10}}))
sys.exit(1 if fail else 0)
""")
        fake.chmod(0o755)
        self.saved = (dispatch.CLAUDE_BIN, dispatch.CLAUDE_MEMBERS, dispatch.CLAUDE_NAMES, dispatch.call, dispatch.render_claude)
        dispatch.CLAUDE_BIN, dispatch.CLAUDE_MEMBERS = str(fake), str(tmp / "members")
        dispatch.CLAUDE_NAMES = {"c2": str(tmp / "acct2")}
        self.posts = []
        dispatch.call = lambda method, path, **kw: self.posts.append((path, kw)) or {}
        dispatch.render_claude = lambda room, session, events: "房間的新訊息"

    def tearDown(self):
        (dispatch.CLAUDE_BIN, dispatch.CLAUDE_MEMBERS, dispatch.CLAUDE_NAMES, dispatch.call, dispatch.render_claude) = self.saved
        os.environ.pop("FAKE_FAIL", None)
        self._tmp.cleanup()

    def calls(self):
        return [json.loads(l) for l in self.record.read_text().splitlines()]

    def test_first_turn_opens_then_resumes_with_account_model_effort(self):
        session, home = dispatch.spawn_claude(str(self.work), "c2", "sonnet", "high")
        self.assertEqual(home, dispatch.CLAUDE_NAMES["c2"])
        worker = dispatch.HeadlessClaudeWorker(session)
        worker.turn("R1", [{"seq": 3}])
        worker.turn("R1", [{"seq": 5}])
        first, second = self.calls()
        self.assertIn("--session-id", first["args"]); self.assertNotIn("--resume", first["args"])
        self.assertIn("--resume", second["args"]); self.assertNotIn("--session-id", second["args"])
        for c in (first, second):
            a = c["args"]
            self.assertEqual(a[a.index("--model") + 1], "sonnet")
            self.assertEqual(a[a.index("--effort") + 1], "high")
            self.assertEqual(a[a.index("--permission-mode") + 1], "auto")
            self.assertTrue(a[a.index("--allowedTools") + 1].endswith("bin/room:*)"))
            self.assertEqual((c["cwd"], c["home"], c["dispatched"]), (str(self.work), home, "1"))
            self.assertEqual(a[-1], "房間的新訊息")
        acks = [kw["seq"] for path, kw in self.posts if path == "/api/ack"]
        self.assertEqual(acks, [3, 5])

    def test_failed_turn_lights_up_and_is_not_marked_started(self):
        session, _ = dispatch.spawn_claude(str(self.work), "c2")
        os.environ["FAKE_FAIL"] = "1"
        dispatch.HeadlessClaudeWorker(session).turn("R1", [{"seq": 2, "from": "u", "wake": [session]}])
        self.assertTrue(any(p == "/api/delivery" and kw["action"] == "failed_after" for p, kw in self.posts))
        self.assertFalse(dispatch.claude_member(session)["started"])

    def test_unknown_account_is_refused(self):
        with self.assertRaises(RuntimeError):
            dispatch.spawn_claude(str(self.work), "nope")
