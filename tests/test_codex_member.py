import json
import os
import tempfile
import unittest
from unittest import mock

from rooms import dispatch


class CodexMemberTest(unittest.TestCase):
    """房間開 Codex 成員時指定模型與推理強度：先驗證帳號有這個模型，只記在這個成員身上，之後每一輪都帶。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sent = []
        models = {"data": [{"model": "gpt-6.1-sol", "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
                           {"model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}]}
        def adapter(kind, sock, request):
            self.sent.append(request)
            if request["method"] == "model/list":
                return models, None
            return {"threadId": "t-1"}, None
        self.patches = [mock.patch.object(dispatch, "adapter", adapter), mock.patch.object(dispatch, "CODEX_MEMBERS", self._tmp.name),
                        mock.patch.object(dispatch, "ACCOUNTS", ["/h/codex"]), mock.patch.object(dispatch, "NAMES", {"/h/codex": "codex"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def test_model_and_effort_are_checked_and_kept_for_this_member(self):
        thread, _ = dispatch.spawn("/work", "codex", "gpt-5.6-sol", "medium")
        start = next(r for r in self.sent if r["method"] == "thread/start")
        self.assertEqual(start["params"], {"cwd": "/work", "model": "gpt-5.6-sol"})
        self.assertEqual(dispatch.codex_member(thread), {"model": "gpt-5.6-sol", "effort": "medium"})

    def test_unknown_model_or_effort_is_refused_before_opening(self):
        for model, effort in (("gpt-9", None), ("gpt-5.6-sol", "ultra")):
            with self.assertRaises(RuntimeError):
                dispatch.spawn("/work", "codex", model, effort)
        self.assertFalse(any(r["method"] == "thread/start" for r in self.sent))

    def test_without_model_nothing_is_recorded(self):
        thread, _ = dispatch.spawn("/work", "codex")
        self.assertEqual(dispatch.codex_member(thread), {})



class DaemonStartTest(unittest.TestCase):
    def test_missing_socket_starts_that_accounts_daemon_once(self):
        # 電腦重開後 /tmp 的 socket 沒了：替那個帳號開背景服務，一分鐘內不重複開
        import tempfile
        from unittest import mock
        from rooms import dispatch
        home = tempfile.mkdtemp()
        with mock.patch.dict(dispatch._daemon_tried, clear=True), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="{}", stderr="")) as run:
            dispatch._sock(home)
            dispatch._sock(home)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], home)
        self.assertEqual(run.call_args.args[0][1:], ["app-server", "daemon", "start"])

if __name__ == "__main__":
    unittest.main()
