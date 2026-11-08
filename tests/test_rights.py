import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from rooms import judge  # noqa: E402
from rooms.core import USER, Core, RoomError  # noqa: E402


class RightsScreen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.core = Core(self._tmp.name)
        self.core.register("s-1", "claude", "結案")
        self.room = self.core.create_room("s-1", "房")["room_id"]
        self.core.move("s-1", "s-1", self.room)
        patch = mock.patch.dict(os.environ, {"ROOMS_JUDGE": "on", "ROOMS_JUDGE_KEY": "k"})
        patch.start()
        self.addCleanup(patch.stop)

    def screen(self, actor, text, prob=0.9, error=None):
        with mock.patch.object(judge, "rights", return_value=(prob, error)) as asked:
            self.core.screen_rights(actor, self.room, text)
        return asked

    def test_warning_is_sent_back_with_the_reason_and_logged(self):
        with self.assertRaises(RoomError) as got:
            self.screen("s-1", "這兩段音檔的權利還沒釐清，建議先問過歌者再用。")
        self.assertIn("原文沒送出", str(got.exception))
        self.assertEqual(got.exception.status if hasattr(got.exception, "status") else 422, 422)
        rows = [json.loads(l) for l in (pathlib.Path(self._tmp.name) / "rights_blocked.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["name"], "結案")
        self.assertIn("音檔的權利", rows[0]["text"])

    def test_below_threshold_passes(self):
        self.screen("s-1", "片尾字卡：〈晚到開園〉江奕霖，以本計畫 AI 音樂模組製作。", prob=0.43)

    def test_judge_down_lets_it_through(self):
        self.screen("s-1", "這兩段音檔的權利還沒釐清，建議先問過歌者再用。", prob=None, error="timeout")

    def test_user_and_short_messages_are_not_asked(self):
        self.assertFalse(self.screen(USER, "這兩段音檔的權利還沒釐清，建議先問過歌者再用。").called)
        self.assertFalse(self.screen("s-1", "收到").called)

    def test_judge_off_is_not_asked(self):
        with mock.patch.dict(os.environ, {"ROOMS_JUDGE": "off"}):
            self.assertFalse(self.screen("s-1", "這兩段音檔的權利還沒釐清，建議先問過歌者再用。").called)


if __name__ == "__main__":
    unittest.main()
