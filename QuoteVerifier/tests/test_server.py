import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from quote_verifier.core import Verifier
from quote_verifier.server import make_handler
from quote_verifier.sources import Paper, SourceError


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Mock()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Verifier(cls.source)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.source.reset_mock()
        self.source.fetch.side_effect = None
        self.source.fetch.return_value = Paper("1706.03762v1", "https://arxiv.org/pdf/1706.03762v1",
                                              ("The exact quote.",), "b" * 64, "2026-11-11")

    def request(self, path, payload=None, method="POST", content_type="application/json", raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        body = raw if raw is not None else json.dumps(payload).encode()
        connection.request(method, path, body=body, headers={"Content-Type": content_type})
        response = connection.getresponse()
        status, value = response.status, json.loads(response.read())
        connection.close()
        return status, value

    def test_boolean_endpoint(self):
        self.assertEqual(self.request("/verify", {"arxiv_id": "1706.03762v1", "quote": "exact quote"}), (200, True))
        self.assertEqual(self.request("/verify", {"arxiv_id": "1706.03762v1", "quote": "invented"}), (200, False))

    def test_detail_endpoint(self):
        status, body = self.request("/verify/details", {"arxiv_id": "1706.03762v1", "quote": "exact quote"})
        self.assertEqual(status, 200)
        self.assertEqual(body["match"]["page_start"], 1)
        self.assertTrue(body["found"])

    def test_invalid_request_before_network(self):
        for payload in [[], {}, {"arxiv_id": "x"}, {"arxiv_id": "x", "quote": ""},
                        {"arxiv_id": "x", "quote": "quote", "extra": 1}]:
            self.assertEqual(self.request("/verify", payload)[0], 400)
        self.source.fetch.assert_not_called()
        self.assertEqual(self.request("/verify", raw=b"{")[0], 400)
        self.assertEqual(self.request("/verify", content_type="text/plain")[0], 415)
        self.assertEqual(self.request("/verify", raw=b"x" * 100001)[0], 413)

    def test_fetch_error_is_non_boolean(self):
        self.source.fetch.side_effect = SourceError("Offline", code="source_unavailable", status=502)
        status, body = self.request("/verify", {"arxiv_id": "1706.03762v1", "quote": "quote"})
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "source_unavailable")

    def test_unexpected_error_is_non_boolean(self):
        self.source.fetch.side_effect = RuntimeError("private detail")
        status, body = self.request("/verify", {"arxiv_id": "1706.03762v1", "quote": "quote"})
        self.assertEqual(status, 500)
        self.assertNotIn("private detail", json.dumps(body))

    def test_health_and_unknown(self):
        self.assertEqual(self.request("/health", method="GET"), (200, {"status": "ok"}))
        self.assertEqual(self.request("/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
