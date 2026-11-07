import datetime
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="rooms-test-data-")
os.environ.setdefault("ROOMS_DATA_DIR", _tmp)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from rooms import cli  # noqa: E402
from rooms.core import Core, RoomError  # noqa: E402
from rooms.identity import USER  # noqa: E402


class Calendar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = Core(pathlib.Path(self.tmp.name))

    def test_add_sorts_lists_and_survives_a_restart(self):
        self.core.calendar_add(USER, "2030-01-02", "晚的", "15:00")
        first = self.core.calendar_add(USER, "2030-01-02", "早的", "09:00-10:00", "備註")
        self.core.calendar_add(USER, "2030-01-01", "前一天")
        self.assertEqual([e["title"] for e in self.core.calendar()], ["前一天", "早的", "晚的"])
        self.assertEqual(Core(pathlib.Path(self.tmp.name)).calendar()[1]["id"], first["id"])
        self.assertEqual(self.core.overview()["calendar"][0]["title"], "前一天")

    def test_bad_input_is_refused(self):
        for date, time_, title in (("明天", "", "x"), ("2030-02-30", "", "x"), ("2030-01-01", "25:00", "x"), ("2030-01-01", "", " ")):
            with self.assertRaises(RoomError):
                self.core.calendar_add(USER, date, title, time_)

    def test_remove(self):
        event = self.core.calendar_add(USER, "2030-01-01", "x")
        self.core.calendar_remove(USER, event["id"])
        self.assertEqual(self.core.calendar(), [])
        with self.assertRaises(RoomError):
            self.core.calendar_remove(USER, event["id"])

    def test_ids_are_not_reused_after_delete(self):
        one = self.core.calendar_add(USER, "2030-01-01", "x")
        self.core.calendar_remove(USER, one["id"])
        self.assertNotEqual(self.core.calendar_add(USER, "2030-01-01", "y")["id"], one["id"])

    def test_hook_text_shows_only_the_next_two_weeks(self):
        today = datetime.date.today()
        day = lambda n: (today + datetime.timedelta(days=n)).isoformat()
        events = [{"id": "C1", "date": day(-1), "time": "", "title": "昨天的", "note": ""},
                  {"id": "C2", "date": day(0), "time": "14:00", "title": "今天的", "note": "帶筆電"},
                  {"id": "C3", "date": day(30), "time": "", "title": "很久以後", "note": ""}]
        text = cli.calendar_text({"calendar": events})
        self.assertIn("今天的", text)
        self.assertIn("帶筆電", text)
        self.assertIn("[C2]", text)
        self.assertNotIn("昨天的", text)
        self.assertNotIn("很久以後", text)
        self.assertEqual(cli.calendar_text({"calendar": []}), "")


if __name__ == "__main__":
    unittest.main()
