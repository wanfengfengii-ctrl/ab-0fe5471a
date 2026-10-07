"""API smoke test against a running service instance.

Usage: python tests/smoke.py [base-url]   (default http://127.0.0.1:8000)
Exits 0 when every check passes, 1 otherwise.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILURES = []


def check(name, condition, extra=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", name,
                         (" :: %s" % extra) if (extra and not condition) else ""))
    if not condition:
        FAILURES.append(name)


def request(method, path, body=None, raw=None):
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        payload = err.read()
        try:
            return err.code, json.loads(payload.decode("utf-8"))
        except ValueError:
            return err.code, None


VALID_BATCH = {
    "participants": ["edge-a", "edge-b", "edge-c"],
    "events": [
        {"id": "a1", "participant": "edge-a", "seq": 1,
         "clock": {"edge-a": 1, "edge-b": 0, "edge-c": 0}},
        {"id": "b1", "participant": "edge-b", "seq": 1,
         "clock": {"edge-a": 1, "edge-b": 1, "edge-c": 0}},
        {"id": "c1", "participant": "edge-c", "seq": 1,
         "clock": {"edge-a": 1, "edge-b": 0, "edge-c": 1}},
        {"id": "a2", "participant": "edge-a", "seq": 2,
         "clock": {"edge-a": 2, "edge-b": 1, "edge-c": 1}},
    ],
}


def main():
    status, payload = request("GET", "/healthz")
    check("healthz returns 200 ok",
          status == 200 and payload.get("status") == "ok",
          "status=%r payload=%r" % (status, payload))

    status, payload = request("POST", "/api/events/audit", body=VALID_BATCH)
    ok = (
        status == 200
        and payload.get("event_count") == 4
        and payload.get("layer_count") == 3
        and [l["event_ids"] for l in payload.get("layers", [])]
            == [["a1"], ["b1", "c1"], ["a2"]]
        and payload.get("merge_order") == ["a1", "b1", "c1", "a2"]
        and payload.get("concurrent_pairs") == 1
    )
    check("valid batch returns expected causal plan", ok,
          "status=%r payload=%r" % (status, payload))

    shuffled = dict(VALID_BATCH,
                    participants=list(reversed(VALID_BATCH["participants"])),
                    events=list(reversed(VALID_BATCH["events"])))
    _, again = request("POST", "/api/events/audit", body=shuffled)
    check("input permutation does not change the result", again == payload,
          "shuffled=%r" % (again,))

    gap = {
        "participants": ["edge-a", "edge-b"],
        "events": [
            {"id": "a1", "participant": "edge-a", "seq": 1,
             "clock": {"edge-a": 1, "edge-b": 0}},
            {"id": "a3", "participant": "edge-a", "seq": 3,
             "clock": {"edge-a": 3, "edge-b": 0}},
        ],
    }
    status, payload = request("POST", "/api/events/audit", body=gap)
    check("sequence gap rejected with 422 SEQUENCE_GAP",
          status == 422 and payload.get("error", {}).get("code")
          == "SEQUENCE_GAP" and payload["error"].get("event_ids"),
          "status=%r payload=%r" % (status, payload))

    cycle = {
        "participants": ["edge-a", "edge-b"],
        "events": [
            {"id": "a1", "participant": "edge-a", "seq": 1,
             "clock": {"edge-a": 1, "edge-b": 1}},
            {"id": "b1", "participant": "edge-b", "seq": 1,
             "clock": {"edge-a": 1, "edge-b": 1}},
        ],
    }
    status, payload = request("POST", "/api/events/audit", body=cycle)
    check("circular dependency rejected with 422 CIRCULAR_DEPENDENCY",
          status == 422 and payload.get("error", {}).get("code")
          == "CIRCULAR_DEPENDENCY",
          "status=%r payload=%r" % (status, payload))

    future = {
        "participants": ["edge-a", "edge-b"],
        "events": [
            {"id": "a1", "participant": "edge-a", "seq": 1,
             "clock": {"edge-a": 1, "edge-b": 5}},
            {"id": "b1", "participant": "edge-b", "seq": 1,
             "clock": {"edge-a": 0, "edge-b": 1}},
        ],
    }
    status, payload = request("POST", "/api/events/audit", body=future)
    check("future reference rejected with 422 FUTURE_REFERENCE",
          status == 422 and payload.get("error", {}).get("code")
          == "FUTURE_REFERENCE",
          "status=%r payload=%r" % (status, payload))

    status, payload = request("POST", "/api/events/audit", raw=b"{broken")
    check("malformed JSON rejected with 400 MALFORMED_JSON",
          status == 400 and payload.get("error", {}).get("code")
          == "MALFORMED_JSON", "status=%r" % status)

    status, payload = request("POST", "/api/events/audit",
                              raw=b" " * (1024 * 1024 + 1))
    check("body over 1 MiB rejected with 413 PAYLOAD_TOO_LARGE",
          status == 413 and payload.get("error", {}).get("code")
          == "PAYLOAD_TOO_LARGE", "status=%r" % status)

    status, payload = request("GET", "/does-not-exist")
    check("unknown path returns 404",
          status == 404 and payload.get("error", {}).get("code") == "NOT_FOUND",
          "status=%r" % status)

    if FAILURES:
        print("smoke: %d check(s) failed" % len(FAILURES))
        return 1
    print("smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
