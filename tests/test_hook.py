import os
import unittest
from unittest import mock

from rooms import cli


class AutoRegisterTest(unittest.TestCase):
    """沒登記過的對話：使用者開口就登記成一個點，名字取工作資料夾，在做什麼取目標或第一句。"""

    def run_register(self, payload, acc=None, env="cli"):
        sent = []
        def quick(method, path, params):
            sent.append((path, dict(params)))
            return {"session": params.get("session")}
        with mock.patch.object(cli, "_quick", quick), mock.patch.object(cli, "acc_session", lambda s: acc or {}), \
                mock.patch.object(cli, "kind_of", lambda s: "claude"), mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": env}):
            cli.auto_register("s-1", payload)
        return sent

    def test_name_from_folder_and_role_from_objective(self):
        sent = self.run_register({"cwd": "/work/lyrics", "prompt": "幫我看"}, {"objective": "我要做寫歌器"})
        self.assertEqual(sent[0], ("/api/register", {"session": "s-1", "kind": "claude", "name": "lyrics"}))
        self.assertEqual(sent[1][1]["role"], "我要做寫歌器")
        self.assertEqual(sent[1][1]["as"], "s-1")                          # 本人改自己的，不用使用者的身分

    def test_home_folder_keeps_default_name_and_uses_prompt(self):
        sent = self.run_register({"cwd": os.path.expanduser("~"), "prompt": "找一下八月的對話"})
        self.assertNotIn("name", sent[0][1])
        self.assertEqual(sent[1][1]["role"], "找一下八月的對話")

    def test_program_runs_are_not_registered(self):
        self.assertEqual(self.run_register({"cwd": "/work/x"}, env="sdk-cli"), [])


    def test_dispatcher_run_skips_the_room_hook(self):
        """背景 Claude 成員那一輪：訊息已經帶在 prompt 裡，hook 不再注入、也不登記。"""
        called = []
        with mock.patch.object(cli, "_quick", lambda *a: called.append(a)), mock.patch.dict(os.environ, {"ROOMS_DISPATCHED": "1"}), \
                mock.patch("sys.stdin", mock.Mock(read=lambda: '{"session_id": "s-1", "hook_event_name": "UserPromptSubmit"}')):
            cli.hook()
        self.assertEqual(called, [])

    def test_session_start_waits_for_the_room_like_stop(self):
        """剛 resume、還沒跑過一輪的對話：SessionStart 就掛上等待，不用等使用者先打字。"""
        for event in ("SessionStart", "Stop"):
            with mock.patch.object(cli, "rewake") as wait, \
                    mock.patch("sys.stdin", mock.Mock(read=lambda: '{"session_id": "s-1", "hook_event_name": "%s"}' % event)):
                cli.hook()
            wait.assert_called_once_with("s-1")


if __name__ == "__main__":
    unittest.main()
