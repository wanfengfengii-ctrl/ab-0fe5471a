"""Unit tests for the audit core: validation rules, layering, concurrency."""

import random
import unittest

from app.audit import (
    AuditError,
    MAX_EVENTS,
    MAX_PARTICIPANTS,
    audit_batch,
)


def make_event(eid, participant, seq, clock):
    return {"id": eid, "participant": participant, "seq": seq, "clock": clock}


def two_party_batch():
    """alpha: a1 -> a2 (a2 saw b1); beta: b1 (saw a1) -> b2 (saw a2)."""
    participants = ["alpha", "beta"]
    events = [
        make_event("a1", "alpha", 1, {"alpha": 1, "beta": 0}),
        make_event("b1", "beta", 1, {"alpha": 1, "beta": 1}),
        make_event("a2", "alpha", 2, {"alpha": 2, "beta": 1}),
        make_event("b2", "beta", 2, {"alpha": 2, "beta": 2}),
    ]
    return participants, events


def gen_valid_batch(rng, n_participants, max_events_per_participant):
    """Simulate real vector-clock traffic so batches are always valid."""
    parts = ["p%02d" % i for i in range(n_participants)]
    clocks = {p: {q: 0 for q in parts} for p in parts}
    seqs = {p: 0 for p in parts}
    events, counter = [], 0
    for p in parts:
        for _ in range(rng.randint(0, max_events_per_participant)):
            others = [q for q in parts if q != p]
            if others and rng.random() < 0.6:
                q = rng.choice(others)
                for r in parts:
                    clocks[p][r] = max(clocks[p][r], clocks[q][r])
            seqs[p] += 1
            clocks[p][p] = seqs[p]
            counter += 1
            events.append(make_event(
                "e%04d" % counter, p, seqs[p], dict(clocks[p])))
    if not events:
        seqs[parts[0]] = 1
        clocks[parts[0]][parts[0]] = 1
        events.append(make_event("e0001", parts[0], 1,
                                 dict(clocks[parts[0]])))
    rng.shuffle(events)
    return parts, events


def brute_force_plan(events):
    """O(n^2) reference implementation straight from clock domination."""

    def dominated(a, b):
        le = all(a["clock"][k] <= b["clock"][k] for k in a["clock"])
        lt = any(a["clock"][k] < b["clock"][k] for k in a["clock"])
        return le and lt

    layer = {}
    for e in sorted(events, key=lambda x: sum(x["clock"].values())):
        preds = [o for o in events if o["id"] != e["id"] and dominated(o, e)]
        layer[e["id"]] = 1 + max([layer[o["id"]] for o in preds], default=-1)
    concurrent = 0
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if not dominated(a, b) and not dominated(b, a):
                concurrent += 1
    return layer, concurrent


class ValidBatchTests(unittest.TestCase):
    def test_chain_layers_and_counts(self):
        participants, events = two_party_batch()
        result = audit_batch({"participants": participants,
                              "events": events})
        self.assertEqual(result["event_count"], 4)
        self.assertEqual(result["layer_count"], 4)
        self.assertEqual([l["event_ids"] for l in result["layers"]],
                         [["a1"], ["b1"], ["a2"], ["b2"]])
        self.assertEqual(result["merge_order"], ["a1", "b1", "a2", "b2"])
        self.assertEqual(result["concurrent_pairs"], 0)

    def test_concurrent_events_share_layer_zero(self):
        result = audit_batch({
            "participants": ["alpha", "beta"],
            "events": [
                make_event("a1", "alpha", 1, {"alpha": 1, "beta": 0}),
                make_event("b1", "beta", 1, {"alpha": 0, "beta": 1}),
            ],
        })
        self.assertEqual(result["layer_count"], 1)
        self.assertEqual(result["layers"][0]["event_ids"], ["a1", "b1"])
        self.assertEqual(result["concurrent_pairs"], 1)

    def test_diamond(self):
        clock = lambda a, b, c, d: {"A": a, "B": b, "C": c, "D": d}
        result = audit_batch({
            "participants": ["A", "B", "C", "D"],
            "events": [
                make_event("a1", "A", 1, clock(1, 0, 0, 0)),
                make_event("b1", "B", 1, clock(1, 1, 0, 0)),
                make_event("c1", "C", 1, clock(1, 0, 1, 0)),
                make_event("d1", "D", 1, clock(1, 1, 1, 1)),
            ],
        })
        self.assertEqual([l["event_ids"] for l in result["layers"]],
                         [["a1"], ["b1", "c1"], ["d1"]])
        self.assertEqual(result["concurrent_pairs"], 1)  # b1 || c1

    def test_within_layer_sorting_is_participant_seq_id(self):
        # zulu's first event and alpha's first two events are all concurrent.
        result = audit_batch({
            "participants": ["zulu", "alpha"],
            "events": [
                make_event("z1", "zulu", 1, {"zulu": 1, "alpha": 0}),
                make_event("a2", "alpha", 2, {"zulu": 0, "alpha": 2}),
                make_event("a1", "alpha", 1, {"zulu": 0, "alpha": 1}),
            ],
        })
        self.assertEqual(result["layers"][0]["event_ids"], ["a1", "z1"])
        self.assertEqual(result["layers"][1]["event_ids"], ["a2"])
        self.assertEqual(result["merge_order"], ["a1", "z1", "a2"])

    def test_input_permutation_does_not_change_result(self):
        participants, events = two_party_batch()
        baseline = audit_batch({"participants": participants,
                                "events": events})
        rng = random.Random(7)
        for _ in range(25):
            shuffled_events = events[:]
            shuffled_participants = participants[:]
            rng.shuffle(shuffled_events)
            rng.shuffle(shuffled_participants)
            again = audit_batch({"participants": shuffled_participants,
                                 "events": shuffled_events})
            self.assertEqual(again, baseline)

    def test_participant_boundaries(self):
        for n in (2, 3, MAX_PARTICIPANTS):
            parts = ["p%02d" % i for i in range(n)]
            events = [make_event("e%d" % i, p, 1,
                                 {q: (1 if q == p else 0) for q in parts})
                      for i, p in enumerate(parts)]
            result = audit_batch({"participants": parts, "events": events})
            self.assertEqual(result["event_count"], n)
            self.assertEqual(result["layer_count"], 1)
            self.assertEqual(result["concurrent_pairs"], n * (n - 1) // 2)

    def test_max_event_volume(self):
        parts = ["p%02d" % i for i in range(MAX_PARTICIPANTS)]
        events = []
        per, extra = divmod(MAX_EVENTS, len(parts))
        for i, p in enumerate(parts):
            count = per + (1 if i < extra else 0)
            for seq in range(1, count + 1):
                clock = {q: 0 for q in parts}
                clock[p] = seq
                events.append(make_event("%s-%d" % (p, seq), p, seq, clock))
        self.assertEqual(len(events), MAX_EVENTS)
        result = audit_batch({"participants": parts, "events": events})
        self.assertEqual(result["event_count"], MAX_EVENTS)
        self.assertEqual(result["layer_count"], per + 1)

    def test_participant_with_zero_events_is_allowed(self):
        result = audit_batch({
            "participants": ["alpha", "beta", "idle"],
            "events": [make_event("a1", "alpha", 1,
                                  {"alpha": 1, "beta": 0, "idle": 0})],
        })
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["layer_count"], 1)


class PropertyTests(unittest.TestCase):
    """Cross-check the optimized plan against the O(n^2) definition."""

    def test_random_valid_batches_match_brute_force(self):
        rng = random.Random(20261007)
        for trial in range(60):
            parts, events = gen_valid_batch(
                rng, rng.randint(2, 6), rng.randint(1, 8))
            result = audit_batch({"participants": parts, "events": events})

            expected_layers, expected_concurrent = brute_force_plan(events)
            actual_layers = {}
            for layer in result["layers"]:
                for eid in layer["event_ids"]:
                    actual_layers[eid] = layer["index"]
            self.assertEqual(actual_layers, expected_layers,
                             "layer mismatch in trial %d" % trial)
            self.assertEqual(result["concurrent_pairs"], expected_concurrent,
                             "concurrency mismatch in trial %d" % trial)

            # merge_order must be a valid topological linearization
            position = {eid: i for i, eid in
                        enumerate(result["merge_order"])}
            self.assertEqual(len(position), len(events))
            by_id = {e["id"]: e for e in events}
            for e in events:
                for q, v in e["clock"].items():
                    if v == 0:
                        continue
                    pred = next(o for o in events
                                if o["participant"] == q and o["seq"] == v)
                    if pred["id"] == e["id"]:
                        continue
                    self.assertLess(position[pred["id"]], position[e["id"]])

            # determinism under reshuffling
            shuffled = events[:]
            rng.shuffle(shuffled)
            self.assertEqual(
                audit_batch({"participants": parts, "events": shuffled}),
                result)


class ErrorTests(unittest.TestCase):
    def assert_rejected(self, payload, code):
        with self.assertRaises(AuditError) as ctx:
            audit_batch(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, 422)
        return ctx.exception

    def test_non_object_body(self):
        self.assert_rejected([1, 2, 3], "INVALID_SCHEMA")

    def test_missing_fields(self):
        self.assert_rejected({"participants": ["a", "b"]}, "INVALID_SCHEMA")

    def test_participant_count_out_of_range(self):
        self.assert_rejected(
            {"participants": ["only"], "events": []},
            "PARTICIPANT_COUNT_OUT_OF_RANGE")
        self.assert_rejected(
            {"participants": ["p%02d" % i for i in range(33)], "events": []},
            "PARTICIPANT_COUNT_OUT_OF_RANGE")

    def test_invalid_participant_entry(self):
        self.assert_rejected({"participants": ["a", ""], "events": []},
                             "INVALID_PARTICIPANT")
        self.assert_rejected({"participants": ["a", 5], "events": []},
                             "INVALID_PARTICIPANT")

    def test_duplicate_participant(self):
        self.assert_rejected({"participants": ["a", "a"], "events": []},
                             "DUPLICATE_PARTICIPANT")

    def test_event_count_out_of_range(self):
        self.assert_rejected({"participants": ["a", "b"], "events": []},
                             "EVENT_COUNT_OUT_OF_RANGE")
        too_many = [make_event("e%d" % i, "a", 1, {"a": 1, "b": 0})
                    for i in range(MAX_EVENTS + 1)]
        self.assert_rejected({"participants": ["a", "b"],
                              "events": too_many},
                             "EVENT_COUNT_OUT_OF_RANGE")

    def test_invalid_event_schema(self):
        base = {"participants": ["a", "b"]}
        self.assert_rejected({**base, "events": ["nope"]},
                             "INVALID_EVENT_SCHEMA")
        self.assert_rejected({**base, "events": [{"id": "x"}]},
                             "INVALID_EVENT_SCHEMA")
        self.assert_rejected({**base, "events": [
            make_event("x", "a", "1", {"a": 1, "b": 0})]},
            "INVALID_EVENT_SCHEMA")
        self.assert_rejected({**base, "events": [
            make_event("x", "a", True, {"a": 1, "b": 0})]},
            "INVALID_EVENT_SCHEMA")

    def test_duplicate_event_id(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("dup", "a", 1, {"a": 1, "b": 0}),
                make_event("dup", "b", 1, {"a": 0, "b": 1}),
            ]}, "DUPLICATE_EVENT_ID")
        self.assertEqual(err.event_ids, ["dup"])

    def test_unknown_participant(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [make_event("x", "ghost", 1, {"a": 0, "b": 0})]},
            "UNKNOWN_PARTICIPANT")
        self.assertEqual(err.event_ids, ["x"])

    def test_invalid_sequence(self):
        self.assert_rejected({
            "participants": ["a", "b"],
            "events": [make_event("x", "a", 0, {"a": 0, "b": 0})]},
            "INVALID_SEQUENCE")

    def test_invalid_clock(self):
        base = {"participants": ["a", "b"]}
        # missing participant
        self.assert_rejected({**base, "events": [
            make_event("x", "a", 1, {"a": 1})]}, "INVALID_CLOCK")
        # extra participant
        self.assert_rejected({**base, "events": [
            make_event("x", "a", 1, {"a": 1, "b": 0, "c": 0})]},
            "INVALID_CLOCK")
        # negative value
        self.assert_rejected({**base, "events": [
            make_event("x", "a", 1, {"a": 1, "b": -1})]}, "INVALID_CLOCK")
        # non-integer value
        self.assert_rejected({**base, "events": [
            make_event("x", "a", 1, {"a": 1, "b": 0.5})]}, "INVALID_CLOCK")

    def test_clock_self_mismatch(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [make_event("x", "a", 1, {"a": 2, "b": 0})]},
            "CLOCK_SELF_MISMATCH")
        self.assertEqual(err.event_ids, ["x"])

    def test_sequence_gap(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 0}),
                make_event("a3", "a", 3, {"a": 3, "b": 0}),
            ]}, "SEQUENCE_GAP")
        self.assertEqual(err.event_ids, ["a1", "a3"])

    def test_sequence_duplicate_is_a_gap(self):
        self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 0}),
                make_event("a1b", "a", 1, {"a": 1, "b": 0}),
            ]}, "SEQUENCE_GAP")

    def test_clock_regression(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 1}),
                make_event("a2", "a", 2, {"a": 2, "b": 0}),
                make_event("b1", "b", 1, {"a": 0, "b": 1}),
            ]}, "CLOCK_REGRESSION")
        self.assertIn("a2", err.event_ids)

    def test_future_reference(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 2}),
                make_event("b1", "b", 1, {"a": 0, "b": 1}),
            ]}, "FUTURE_REFERENCE")
        self.assertEqual(err.event_ids, ["a1"])

    def test_circular_dependency(self):
        err = self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 1}),
                make_event("b1", "b", 1, {"a": 1, "b": 1}),
            ]}, "CIRCULAR_DEPENDENCY")
        self.assertEqual(err.event_ids, ["a1", "b1"])

    def test_circular_dependency_longer_cycle(self):
        err = self.assert_rejected({
            "participants": ["a", "b", "c"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 0, "c": 1}),
                make_event("b1", "b", 1, {"a": 1, "b": 1, "c": 0}),
                make_event("c1", "c", 1, {"a": 0, "b": 1, "c": 1}),
            ]}, "CIRCULAR_DEPENDENCY")
        self.assertEqual(err.event_ids, ["a1", "b1", "c1"])

    def test_domination_violation_without_cycle(self):
        # a1 claims to have seen b1, but b1 saw c1 which a1 never saw.
        err = self.assert_rejected({
            "participants": ["a", "b", "c"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 1, "c": 0}),
                make_event("b1", "b", 1, {"a": 0, "b": 1, "c": 1}),
                make_event("c1", "c", 1, {"a": 0, "b": 0, "c": 1}),
            ]}, "DOMINATION_VIOLATION")
        self.assertEqual(err.event_ids, ["a1", "b1"])

    def test_equal_clocks_on_distinct_events_are_circular(self):
        # identical clocks on different events imply mutual dependency.
        self.assert_rejected({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 1}),
                make_event("b1", "b", 1, {"a": 1, "b": 1}),
            ]}, "CIRCULAR_DEPENDENCY")

    def test_valid_batch_accepted(self):
        # guard: the diamond-shaped small batch stays valid end to end.
        result = audit_batch({
            "participants": ["a", "b"],
            "events": [
                make_event("a1", "a", 1, {"a": 1, "b": 1}),
                make_event("b1", "b", 1, {"a": 0, "b": 1}),
                make_event("b2", "b", 2, {"a": 1, "b": 2}),
                make_event("a2", "a", 2, {"a": 2, "b": 1}),
            ]})
        self.assertEqual(result["event_count"], 4)
        # a2={a:2,b:1} and b2={a:1,b:2} are mutually concurrent.
        self.assertEqual(result["concurrent_pairs"], 1)
        self.assertEqual([l["event_ids"] for l in result["layers"]],
                         [["b1"], ["a1"], ["a2", "b2"]])

    def test_error_payload_shape(self):
        try:
            audit_batch({"participants": ["a"], "events": []})
        except AuditError as err:
            payload = err.to_payload()
            self.assertIn("error", payload)
            self.assertEqual(payload["error"]["code"],
                             "PARTICIPANT_COUNT_OUT_OF_RANGE")
            self.assertIsInstance(payload["error"]["event_ids"], list)
            self.assertIsInstance(payload["error"]["details"], dict)
        else:
            self.fail("expected AuditError")


if __name__ == "__main__":
    unittest.main()
