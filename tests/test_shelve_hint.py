import json
import os
os.environ["ROOMS_JUDGE"] = "off"
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rooms import dispatch
from rooms.core import USER, Core


class ShelveHintTest(unittest.TestCase):
    """判官建議收起下線的對話：做完了、沒有要叫回來的才建議；同一次下線只判一次；使用者說留著就不再建議。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = Core(self._tmp.name)
        self.core.register("s-1", "codex", "份A")
        self.core.set_role(USER, "s-1", "寫素材包知識小卡")
        self.core.set_off(USER, "s-1", True, reason="766 包交件，抽查過")

    def tearDown(self):
        self._tmp.cleanup()

    def judge(self, probs, key="k1"):
        with mock.patch.dict(os.environ, {"ROOMS_JUDGE": "on", "ROOMS_JUDGE_KEY": "k"}), \
                mock.patch("rooms.judge.shelve_hint", return_value=(probs, None)) as ask:
            got = self.core.judge_shelve("", "s-1", {"objective": "我要寫小卡（achieved）", "last": "全部交了"}, key)
        return got, ask

    def test_suggests_only_when_done_and_nothing_pending(self):
        got, ask = self.judge({"done": 0.9, "pending": 0.1})
        self.assertTrue(got["suggest"])
        state = ask.call_args[0][1]
        for piece in ("份A", "寫素材包知識小卡", "766 包交件", "我要寫小卡", "全部交了"):
            self.assertIn(piece, state)
        self.core.state["agents"]["s-1"].pop("shelve_hint")
        self.assertFalse(self.judge({"done": 0.9, "pending": 0.6})[0]["suggest"])   # 還有要接著做的

    def test_same_offline_is_judged_once_and_return_clears(self):
        self.judge({"done": 0.9, "pending": 0.1})
        _, ask = self.judge({"done": 0.1, "pending": 0.9})
        ask.assert_not_called()
        self.core.set_off(USER, "s-1", False)
        self.assertIsNone(self.core.state["agents"]["s-1"].get("shelve_hint"))

    def test_keep_stops_suggesting(self):
        self.judge({"done": 0.9, "pending": 0.1})
        self.core.keep_shelve_hint(USER, "s-1")
        row = next(a for a in self.core.overview()["agents"] if a["session"] == "s-1")
        self.assertFalse(row["shelve_hint"]["suggest"])


class ShelveLabelTest(ShelveHintTest):
    def labels(self):
        path = Path(self._tmp.name, "shelve_labels.jsonl")
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_users_choice_is_recorded_with_what_the_judge_saw(self):
        self.judge({"done": 0.9, "pending": 0.1})
        self.assertEqual(self.labels(), [])                                 # 還沒動作：不記
        self.core.shelve(USER, "s-1", True)
        row = self.labels()[0]
        self.assertEqual((row["label"], row["suggested"], row["done"]), ("shelved", True, 0.9))
        self.assertIn("766 包交件", row["state"])
        self.core.shelve(USER, "s-1", False)
        self.assertEqual([r["label"] for r in self.labels()], ["shelved", "unshelved"])

    def test_keep_is_recorded_as_judge_wrong(self):
        self.judge({"done": 0.9, "pending": 0.1})
        self.core.keep_shelve_hint(USER, "s-1")
        self.assertEqual([r["label"] for r in self.labels()], ["kept"])


class LastWordsTest(unittest.TestCase):
    """從主機上的對話紀錄拿最後一段 agent 說的話（Codex rollout、Claude transcript）。"""

    def test_codex_and_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp, "sessions", "2026", "10", "25"); day.mkdir(parents=True)
            rows = [{"type": "session_meta"},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "做到一半"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "全部交件了"}]}}]
            (day / "rollout-x-abc.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            proj = Path(tmp, "claude", "projects", "-w"); proj.mkdir(parents=True)
            rows = [{"type": "user"}, {"type": "assistant", "message": {"content": [{"type": "text", "text": "報告寫好了"}]}}]
            (proj / "c-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            with mock.patch.object(dispatch, "WSL_SESSIONS", str(Path(tmp, "sessions"))), \
                    mock.patch.object(dispatch, "ACCOUNTS", []), \
                    mock.patch.object(dispatch, "CLAUDE_HOME", str(Path(tmp, "claude"))), \
                    mock.patch.object(dispatch, "CLAUDE_NAMES", {}):
                self.assertEqual(dispatch.last_words("abc", "codex"), "全部交件了")
                self.assertEqual(dispatch.last_words("c-1", "claude"), "報告寫好了")


if __name__ == "__main__":
    unittest.main()
