import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rooms import cli, quota


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        for d, login in ((".claude", ".credentials.json"), (".claude-work", ".credentials.json"), (".codex", "auth.json")):
            (self.home / d).mkdir()
            (self.home / d / login).write_text("{}")
        (self.home / ".codex-empty").mkdir()                          # 沒登入的不算
        self.cfg = self.home / "accounts.json"
        self.p = [mock.patch.object(Path, "home", return_value=self.home), mock.patch.object(quota, "CONFIG", self.cfg),
                  mock.patch.dict(os.environ, {"ROOMS_CLAUDE_HOMES": "", "ROOMS_CODEX_HOMES": ""})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_each_machine_finds_its_own_accounts_and_can_add_more(self):
        self.assertEqual(sorted((a["name"], a["kind"]) for a in quota.accounts()),
                         [("claude", "claude"), ("claude-work", "claude"), ("codex", "codex")])
        quota.upsert({"name": "朋友的", "kind": "codex", "home": str(self.home / ".codex-empty"), "warn": "70"})
        rows = {a["name"]: a for a in quota.accounts()}
        self.assertEqual(rows["朋友的"]["warn"], 70)
        self.assertTrue(self.cfg.exists())
        quota.remove("朋友的")
        self.assertNotIn("朋友的", {a["name"] for a in quota.accounts()})
        with self.assertRaises(ValueError):
            quota.upsert({"name": "x", "kind": "別的", "home": "/tmp"})

    def test_line_flags_the_warning_threshold(self):
        row = {"name": "c", "kind": "codex", "warn": 80, "usage": {"five_hours": {"used": 82, "resets_at": None},
                                                                     "week": {"used": 10, "resets_at": None}}}
        self.assertIn("到提醒線", quota.line(row))
        row["usage"]["five_hours"]["used"] = 40
        self.assertNotIn("到提醒線", quota.line(row))


class SpawnNotesTest(unittest.TestCase):
    def test_reminds_but_never_blocks(self):
        # 同帳號已經 2 個在跑、額度到提醒線、名字是別人的開頭：都只是提醒
        rows = [{"name": "codex", "kind": "codex", "warn": 80, "running": 2, "running_names": ["標1", "標2"],
                 "usage": {"five_hours": {"used": 85}, "week": {"used": 5}}}]
        room = {"members": [{"name": "評分員D"}, {"name": "主持"}]}
        with mock.patch.object(cli, "accounts_now", return_value=rows):
            notes = cli.spawn_notes(room, "評分員", "codex", "/nonexistent", False, None)
        text = "\n".join(notes)
        self.assertIn("已經有 2 個成員在跑", text)
        self.assertIn("提醒線", text)
        self.assertIn("一個是另一個的開頭", text)
        self.assertIn("沒指定 --model", text)


if __name__ == "__main__":
    unittest.main()
