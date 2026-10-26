import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from rooms import local


class LocalHelperTest(unittest.TestCase):
    """本機小幫手綁 127.0.0.1，但任何網頁都連得到：沒帶標頭、來源不在名單的一律擋。"""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), local.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self._p = mock.patch.object(local, "ORIGINS", {"http://localhost:8775"})
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.srv.shutdown()

    def get(self, path, headers):
        req = urllib.request.Request(self.url + path, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, None, e.headers

    def test_needs_header_and_known_origin(self):
        ok = {"X-Rooms-Local": "1", "Origin": "http://localhost:8775"}
        self.assertEqual(self.get("/local/caps", {"Origin": "http://localhost:8775"})[0], 403)
        self.assertEqual(self.get("/local/caps", {"X-Rooms-Local": "1", "Origin": "http://evil.example"})[0], 403)
        with mock.patch.object(local, "caps", return_value={"acc": False, "jl": False}):
            status, body, headers = self.get("/local/caps", ok)
        self.assertEqual((status, body), (200, {"acc": False, "jl": False}))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:8775")

    def test_decide_rejects_unknown_choice(self):
        req = urllib.request.Request(self.url + "/local/decide", data=b'{"choice": "maybe"}', method="POST",
                                     headers={"X-Rooms-Local": "1", "Origin": "http://localhost:8775"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
