"""API-level tests: run the server in-process on an ephemeral port."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import MAX_BODY_BYTES, AuditHandler


def http(method, url, body=None, raw=None, headers=None):
    data = raw
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        payload = err.read()
        try:
            return err.code, json.loads(payload.decode("utf-8"))
        except ValueError:
            return err.code, None


VALID_BATCH = {
    "participants": ["alpha", "beta"],
    "events": [
        {"id": "a1", "participant": "alpha", "seq": 1,
         "clock": {"alpha": 1, "beta": 0}},
        {"id": "b1", "participant": "beta", "seq": 1,
         "clock": {"alpha": 1, "beta": 1}},
        {"id": "a2", "participant": "alpha", "seq": 2,
         "clock": {"alpha": 2, "beta": 1}},
    ],
}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AuditHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_healthz(self):
        status, payload = http("GET", self.base + "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_root(self):
        status, _ = http("GET", self.base + "/")
        self.assertEqual(status, 200)

    def test_valid_audit(self):
        status, payload = http("POST", self.base + "/api/events/audit",
                               body=VALID_BATCH)
        self.assertEqual(status, 200)
        self.assertEqual(payload["event_count"], 3)
        self.assertEqual(payload["layer_count"], 3)
        self.assertEqual(payload["merge_order"], ["a1", "b1", "a2"])
        self.assertEqual(payload["concurrent_pairs"], 0)
        self.assertEqual([l["event_ids"] for l in payload["layers"]],
                         [["a1"], ["b1"], ["a2"]])

    def test_audit_is_permutation_invariant_over_http(self):
        shuffled = {
            "participants": ["beta", "alpha"],
            "events": list(reversed(VALID_BATCH["events"])),
        }
        _, a = http("POST", self.base + "/api/events/audit", body=VALID_BATCH)
        _, b = http("POST", self.base + "/api/events/audit", body=shuffled)
        self.assertEqual(a, b)

    def test_invalid_batch_422_with_code_and_ids(self):
        bad = {
            "participants": ["alpha", "beta"],
            "events": [
                {"id": "a1", "participant": "alpha", "seq": 1,
                 "clock": {"alpha": 1, "beta": 0}},
                {"id": "a3", "participant": "alpha", "seq": 3,
                 "clock": {"alpha": 3, "beta": 0}},
            ],
        }
        status, payload = http("POST", self.base + "/api/events/audit",
                               body=bad)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "SEQUENCE_GAP")
        self.assertEqual(payload["error"]["event_ids"], ["a1", "a3"])

    def test_circular_dependency_over_http(self):
        bad = {
            "participants": ["a", "b"],
            "events": [
                {"id": "a1", "participant": "a", "seq": 1,
                 "clock": {"a": 1, "b": 1}},
                {"id": "b1", "participant": "b", "seq": 1,
                 "clock": {"a": 1, "b": 1}},
            ],
        }
        status, payload = http("POST", self.base + "/api/events/audit",
                               body=bad)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "CIRCULAR_DEPENDENCY")

    def test_malformed_json_400(self):
        status, payload = http("POST", self.base + "/api/events/audit",
                               raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "MALFORMED_JSON")

    def test_oversize_body_413(self):
        raw = b" " * (MAX_BODY_BYTES + 1)
        status, payload = http("POST", self.base + "/api/events/audit",
                               raw=raw)
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "PAYLOAD_TOO_LARGE")

    def test_exactly_one_mib_is_accepted_for_parsing(self):
        # pad a valid JSON document with trailing whitespace to exactly 1 MiB
        doc = json.dumps(VALID_BATCH)
        padded = doc + " " * (MAX_BODY_BYTES - len(doc))
        self.assertEqual(len(padded.encode()), MAX_BODY_BYTES)
        status, payload = http("POST", self.base + "/api/events/audit",
                               raw=padded.encode())
        self.assertEqual(status, 200)
        self.assertEqual(payload["event_count"], 3)

    def test_unknown_path_404(self):
        status, payload = http("GET", self.base + "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_method_not_allowed_405(self):
        status, payload = http("GET", self.base + "/api/events/audit")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "METHOD_NOT_ALLOWED")
        status, _ = http("PUT", self.base + "/api/events/audit",
                         body=VALID_BATCH)
        self.assertEqual(status, 405)

    def test_healthz_survives_parallel_audits(self):
        results = []

        def post():
            results.append(http("POST", self.base + "/api/events/audit",
                                body=VALID_BATCH)[0])

        threads = [threading.Thread(target=post) for _ in range(8)]
        for t in threads:
            t.start()
        status, _ = http("GET", self.base + "/healthz")
        for t in threads:
            t.join()
        self.assertEqual(status, 200)
        self.assertEqual(results, [200] * 8)


if __name__ == "__main__":
    unittest.main()
