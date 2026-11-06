import json
import os
import pathlib
import shlex
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from rooms import judge, keyswitch  # noqa: E402

K1 = "sk-or-v1-" + "a" * 61 + "111"
K2 = "sk-or-v1-" + "b" * 61 + "222"


class KeySwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.dir = self.root / "conf"
        self.dir.mkdir()
        patcher = mock.patch.dict(os.environ, {"ROOMS_KEYS_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.dir / "key1").write_text(K1)
        (self.dir / "key2").write_text(K2)
        self.raw = self.root / "raw.key"
        self.raw.write_text(K1)
        self.env = self.root / "app.env"
        self.env.write_text(f"NAME=x\nPOOL={K1}\nOTHER=sk-or-v1-{'c' * 61}333\n")
        self.env.chmod(0o640)
        self.flag = self.root / "restarted"
        keyswitch._status.update(at=0, rows={})
        self.targets = [{"id": "raw", "project": "甲", "path": str(self.raw), "mode": "raw"},
                        {"id": "env", "project": "乙", "path": str(self.env),
                         "restart": f"echo x >> {shlex.quote(str(self.flag))}"}]
        self.conf()

    def conf(self, **extra):
        (self.dir / "targets.json").write_text(json.dumps({"hosts": {"": {"via": []}}, "targets": self.targets, **extra}))

    def test_switch_changes_every_target_and_keeps_the_rest(self):
        got = keyswitch.switch(2)
        self.assertEqual(self.raw.read_text().strip(), K2)
        text = self.env.read_text()
        self.assertIn(f"POOL={K2}", text)
        self.assertIn("NAME=x", text)
        self.assertIn("c" * 61 + "333", text)                      # 第三把不是開關管的，不動
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o640)
        self.assertTrue(all(r["ok"] for r in got["targets"]))
        self.assertEqual(self.flag.read_text().count("x"), 1)
        self.assertEqual(got["active"], "2")
        keyswitch.switch(1)
        self.assertEqual(self.raw.read_text().strip(), K1)

    def test_primary_and_backup_lines_get_opposite_keys(self):
        self.env.write_text(f"NAME=x\nPOOL={K1}\nPOOL_BACKUP={K2}\n")
        self.targets[1].update(var="POOL", backup_var="POOL_BACKUP")
        self.conf()
        rows = {r["id"]: r for r in keyswitch.switch(2)["targets"]}
        text = self.env.read_text()
        self.assertIn(f"POOL={K2}\n", text)
        self.assertIn(f"POOL_BACKUP={K1}\n", text)
        self.assertIn("NAME=x", text)
        self.assertTrue(rows["env"]["ok"])
        keyswitch.switch(1)
        text = self.env.read_text()
        self.assertIn(f"POOL={K1}\n", text)
        self.assertIn(f"POOL_BACKUP={K2}\n", text)

    def test_service_already_on_the_key_is_not_restarted(self):
        self.env.write_text(f"POOL={K2}\n")
        self.targets[1].update(var="POOL", verify={"proc": "no-such-process", "var": "X", "optional": True})
        self.conf()
        keyswitch.switch(2)
        self.assertFalse(self.flag.exists())

    def test_busy_service_is_retried_until_it_restarts(self):
        busy = self.root / "busy"
        busy.write_text("1")
        self.targets[1]["restart"] = f"test ! -e {shlex.quote(str(busy))} && echo x >> {shlex.quote(str(self.flag))}"
        self.conf()
        got = keyswitch.switch(2)
        self.assertEqual(got["pending"], [""])
        self.assertFalse(self.flag.exists())
        busy.unlink()
        keyswitch.retry_pending()
        self.assertTrue(self.flag.exists())
        self.assertEqual(keyswitch.view()["pending"], [])

    def test_view_never_contains_a_key(self):
        for got in (keyswitch.switch(2), keyswitch.view(True)):
            text = json.dumps(got)
            self.assertNotIn(K1, text)
            self.assertNotIn(K2, text)
            self.assertEqual([k["tail"] for k in got["keys"]], ["111", "222"])

    def test_restart_output_with_a_key_is_scrubbed(self):
        self.targets[1]["restart"] = f"echo {K1}; exit 3"
        self.conf()
        got = keyswitch.switch(2)
        row = next(r for r in got["targets"] if r["id"] == "env")
        self.assertFalse(row["ok"])
        self.assertIn("重啟", row["error"])
        self.assertNotIn(K1, json.dumps(got))

    def test_a_target_left_on_the_old_key_is_reported(self):
        keyswitch.switch(2)
        self.raw.write_text(K1)
        rows = {r["id"]: r for r in keyswitch.view(True)["targets"]}
        self.assertFalse(rows["raw"]["ok"])
        self.assertEqual(rows["raw"]["tails"], ["111"])
        self.assertTrue(rows["env"]["ok"])

    def test_file_without_a_known_key_is_an_error_not_a_silent_skip(self):
        self.env.write_text("NAME=x\n")
        row = next(r for r in keyswitch.switch(2)["targets"] if r["id"] == "env")
        self.assertFalse(row["ok"])
        self.assertTrue(row["error"])
        self.assertFalse(self.flag.exists())                       # 檔案沒換成就不重啟

    def test_offline_host_is_pending_then_filled_in(self):
        conf = {"hosts": {"": {"via": []}, "off": {"via": [str(self.root / "no-such-ssh")]}}, "targets": self.targets
                + [{"id": "far", "host": "off", "path": str(self.root / "far.key"), "mode": "raw"}]}
        (self.root / "far.key").write_text(K1)
        (self.dir / "targets.json").write_text(json.dumps(conf))
        with mock.patch.object(keyswitch, "_run_on", side_effect=self._offline):
            got = keyswitch.switch(2)
        self.assertEqual(got["pending"], ["off"])
        self.assertTrue(next(r for r in got["targets"] if r["id"] == "far")["offline"])
        self.assertTrue(next(r for r in got["targets"] if r["id"] == "raw")["ok"])
        conf["hosts"]["off"]["via"] = []                            # 開機了
        (self.dir / "targets.json").write_text(json.dumps(conf))
        keyswitch.retry_pending()
        self.assertEqual((self.root / "far.key").read_text().strip(), K2)
        self.assertEqual(keyswitch.view()["pending"], [])

    def _offline(self, host, module, payload, timeout):
        if host == "off":
            raise ConnectionError("連不上")
        return self._real(host, module, payload, timeout)

    _real = staticmethod(keyswitch._run_on)

    def test_scan_lists_places_outside_the_list(self):
        keyswitch.switch(1)
        tree = self.root / "tree"
        (tree / "old").mkdir(parents=True)
        (tree / "new.env").write_text(f"X={K2}\n")
        (tree / "old" / "a.bak").write_text(K1)
        (tree / "other.env").write_text(f"sk-or-v1-{'c' * 61}333")
        (tree / "listed.key").write_text(K1)
        self.targets.append({"id": "l", "path": str(tree / "listed.key"), "mode": "raw"})
        (self.dir / "targets.json").write_text(json.dumps({"hosts": {"": {"via": [], "scan": [str(tree)]}}, "targets": self.targets,
                                                           "ignore": [{"match": r"\.bak$", "why": "舊備份"}]}))
        got = keyswitch.scan()
        self.assertEqual([o["where"] for o in got["outside"]], [str(tree / "new.env")])
        self.assertEqual(got["outside"][0]["tails"], ["222"])
        self.assertEqual(got["skipped"], 1)
        self.assertNotIn(K2, json.dumps(got))

    def test_not_configured_shows_nothing(self):
        (self.dir / "key2").unlink()
        self.assertEqual(keyswitch.view(), {"configured": False})


class JudgeKeyFile(unittest.TestCase):
    def test_read_on_every_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "k"
            path.write_text("one\n")
            with mock.patch.dict(os.environ, {"ROOMS_JUDGE_KEY_FILE": str(path), "ROOMS_JUDGE_KEY": "env"}):
                self.assertEqual(judge._key(), "one")
                path.write_text("two\n")
                self.assertEqual(judge._key(), "two")
                path.unlink()
                self.assertEqual(judge._key(), "env")


if __name__ == "__main__":
    unittest.main()
