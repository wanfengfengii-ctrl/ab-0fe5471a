"""Core validation and causal-ordering logic for offline event-batch audits.

Offline inspection terminals record events while disconnected; every event
carries a vector clock covering every participant.  A batch is accepted only
when the clocks describe a complete, acyclic causal history.  Accepted batches
are folded into deterministic "earliest executable" causal layers.

Key fact (valid batches): event ``a`` happens-before event ``b`` iff
``a.seq <= b.clock[a.participant]`` (and ``a != b``).  Strict clock domination
and this relation coincide, so layering and concurrency counting run in
O(events x participants) without pairwise clock scans.
"""

from __future__ import annotations

MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 32
MIN_EVENTS = 1
MAX_EVENTS = 2000


class AuditError(Exception):
    """Deterministic, machine-readable rejection of an event batch."""

    def __init__(self, code, message, event_ids=(), details=None, status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.event_ids = sorted({str(e) for e in event_ids})
        self.details = details or {}
        self.status = status

    def to_payload(self):
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "event_ids": self.event_ids,
                "details": self.details,
            }
        }


def _is_int(value):
    """JSON integer check that rejects booleans and floats."""
    return isinstance(value, int) and not isinstance(value, bool)


class _Event:
    __slots__ = ("id", "participant", "seq", "clock")

    def __init__(self, event_id, participant, seq, clock):
        self.id = event_id
        self.participant = participant
        self.seq = seq
        self.clock = clock  # dict[participant, int]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_participants(payload):
    participants = payload.get("participants")
    if not isinstance(participants, list):
        raise AuditError(
            "INVALID_SCHEMA",
            "'participants' must be a JSON array of unique non-empty strings",
        )
    bad = [i for i, p in enumerate(participants)
           if not isinstance(p, str) or not p]
    if bad:
        raise AuditError(
            "INVALID_PARTICIPANT",
            "every participant must be a non-empty string",
            details={"indexes": bad},
        )
    if not MIN_PARTICIPANTS <= len(participants) <= MAX_PARTICIPANTS:
        raise AuditError(
            "PARTICIPANT_COUNT_OUT_OF_RANGE",
            "participants must contain between %d and %d entries, got %d"
            % (MIN_PARTICIPANTS, MAX_PARTICIPANTS, len(participants)),
        )
    seen, dups = set(), set()
    for p in participants:
        if p in seen:
            dups.add(p)
        seen.add(p)
    if dups:
        raise AuditError(
            "DUPLICATE_PARTICIPANT",
            "participants must be unique",
            details={"participants": sorted(dups)},
        )
    return participants


def _check_event_schema(payload):
    events = payload.get("events")
    if not isinstance(events, list):
        raise AuditError(
            "INVALID_SCHEMA",
            "'events' must be a JSON array of event objects",
        )
    if not MIN_EVENTS <= len(events) <= MAX_EVENTS:
        raise AuditError(
            "EVENT_COUNT_OUT_OF_RANGE",
            "events must contain between %d and %d entries, got %d"
            % (MIN_EVENTS, MAX_EVENTS, len(events)),
        )
    problems = []
    related = []
    for index, raw in enumerate(events):
        problem = None
        if not isinstance(raw, dict):
            problem = "event must be a JSON object"
        else:
            for field in ("id", "participant", "seq", "clock"):
                if field not in raw:
                    problem = "missing field '%s'" % field
                    break
            else:
                if not isinstance(raw["id"], str) or not raw["id"]:
                    problem = "'id' must be a non-empty string"
                elif not isinstance(raw["participant"], str):
                    problem = "'participant' must be a string"
                elif not _is_int(raw["seq"]):
                    problem = "'seq' must be an integer"
                elif not isinstance(raw["clock"], dict):
                    problem = "'clock' must be a JSON object"
        if problem:
            problems.append({"index": index, "reason": problem})
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                related.append(raw["id"])
    if problems:
        raise AuditError(
            "INVALID_EVENT_SCHEMA",
            "one or more events are malformed",
            event_ids=related,
            details={"problems": problems},
        )
    return events


def _parse_events(raw_events, participant_set):
    seen, dups = set(), set()
    for e in raw_events:
        if e["id"] in seen:
            dups.add(e["id"])
        seen.add(e["id"])
    if dups:
        raise AuditError(
            "DUPLICATE_EVENT_ID",
            "event ids must be unique",
            event_ids=sorted(dups),
        )

    unknown = [e for e in raw_events if e["participant"] not in participant_set]
    if unknown:
        raise AuditError(
            "UNKNOWN_PARTICIPANT",
            "event participant is not declared in 'participants'",
            event_ids=[e["id"] for e in unknown],
            details={"participants": sorted({e["participant"]
                                             for e in unknown})},
        )

    bad_seq = [e for e in raw_events if e["seq"] < 1]
    if bad_seq:
        raise AuditError(
            "INVALID_SEQUENCE",
            "'seq' must be an integer >= 1 (local sequence starts at 1)",
            event_ids=[e["id"] for e in bad_seq],
        )

    return [
        _Event(e["id"], e["participant"], e["seq"], e["clock"])
        for e in raw_events
    ]


def _check_clocks(events, participant_set):
    malformed, mismatch = [], []
    for e in events:
        keys = set(e.clock.keys())
        if keys != participant_set:
            malformed.append({
                "id": e.id,
                "reason": "clock must cover exactly the declared participants",
                "missing": sorted(participant_set - keys),
                "extra": sorted(keys - participant_set),
            })
            continue
        bad = [k for k, v in e.clock.items() if not _is_int(v) or v < 0]
        if bad:
            malformed.append({
                "id": e.id,
                "reason": "clock values must be non-negative integers",
                "participants": sorted(bad),
            })
    if malformed:
        raise AuditError(
            "INVALID_CLOCK",
            "vector clocks must cover every participant with non-negative "
            "integer values",
            event_ids=[m["id"] for m in malformed],
            details={"problems": malformed},
        )

    for e in events:
        if e.clock[e.participant] != e.seq:
            mismatch.append(e)
    if mismatch:
        raise AuditError(
            "CLOCK_SELF_MISMATCH",
            "an event's own clock component must equal its sequence number",
            event_ids=[e.id for e in mismatch],
        )


def _group_by_participant(events, participants):
    grouped = {p: [] for p in participants}
    for e in events:
        grouped[e.participant].append(e)
    for p in participants:
        grouped[p].sort(key=lambda e: (e.seq, e.id))
    return grouped


def _check_sequences(grouped, participants):
    gaps = {}
    related = []
    for p in sorted(participants):
        seqs = [e.seq for e in grouped[p]]
        if seqs != list(range(1, len(seqs) + 1)):
            gaps[p] = {"expected": list(range(1, len(seqs) + 1)), "actual": seqs}
            related.extend(e.id for e in grouped[p])
    if gaps:
        raise AuditError(
            "SEQUENCE_GAP",
            "each participant's sequence numbers must be consecutive "
            "starting from 1 (no gaps, no duplicates)",
            event_ids=related,
            details={"participants": gaps},
        )


def _check_regression(grouped, participants):
    regressions = []
    related = []
    for p in sorted(participants):
        chain = grouped[p]
        for prev, cur in zip(chain, chain[1:]):
            shrunk = sorted(q for q in cur.clock
                            if cur.clock[q] < prev.clock[q])
            if shrunk:
                regressions.append({
                    "participant": p,
                    "previous": prev.id,
                    "current": cur.id,
                    "regressed_components": shrunk,
                })
                related.extend([prev.id, cur.id])
    if regressions:
        raise AuditError(
            "CLOCK_REGRESSION",
            "vector clocks of a single participant must never regress "
            "(successor clocks must be component-wise non-decreasing)",
            event_ids=related,
            details={"regressions": regressions},
        )


def _check_references_exist(events, counts):
    dangling = []
    related = []
    for e in sorted(events, key=lambda x: x.id):
        for q in sorted(e.clock):
            if e.clock[q] > counts[q]:
                dangling.append({
                    "event": e.id,
                    "participant": q,
                    "referenced_seq": e.clock[q],
                    "known_events": counts[q],
                })
                related.append(e.id)
    if dangling:
        raise AuditError(
            "FUTURE_REFERENCE",
            "an event claims to have seen an event that does not exist "
            "in this batch",
            event_ids=related,
            details={"references": dangling},
        )


def _reference_edges(events):
    """Dependency edges: e depends on its causal predecessors."""
    edges = {}
    for e in events:
        deps = []
        if e.seq > 1:
            deps.append((e.participant, e.seq - 1))
        for q, v in e.clock.items():
            if q != e.participant and v > 0:
                deps.append((q, v))
        edges[(e.participant, e.seq)] = deps
    return edges


def _check_acyclic(events, edges, by_key):
    """Iterative Tarjan SCC; every SCC larger than one node is a cycle."""
    index_of, lowlink, on_stack = {}, {}, set()
    stack, sccs = [], []
    counter = 0
    keys = [(e.participant, e.seq) for e in events]
    for root in keys:
        if root in index_of:
            continue
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(edges[root]))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(edges[nxt])))
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                if len(scc) > 1:
                    sccs.append(scc)
    if sccs:
        cycles = sorted(
            (sorted(by_key[k].id for k in scc) for scc in sccs),
            key=lambda ids: (ids[0], len(ids)),
        )
        related = [eid for cycle in cycles for eid in cycle]
        raise AuditError(
            "CIRCULAR_DEPENDENCY",
            "vector clocks form a circular causal dependency; no valid "
            "execution order exists",
            event_ids=related,
            details={"cycles": cycles},
        )


def _check_domination(events, by_key):
    violations = []
    related = []
    for e in sorted(events, key=lambda x: x.id):
        for q in sorted(e.clock):
            v = e.clock[q]
            if q == e.participant or v == 0:
                continue
            ref = by_key[(q, v)]
            covered = all(ref.clock[r] <= e.clock[r] for r in e.clock)
            strict = any(ref.clock[r] < e.clock[r] for r in e.clock)
            if not (covered and strict):
                violations.append({
                    "event": e.id,
                    "referenced": ref.id,
                    "participant": q,
                    "referenced_seq": v,
                })
                related.extend([e.id, ref.id])
    if violations:
        raise AuditError(
            "DOMINATION_VIOLATION",
            "a referenced event's clock must be covered component-wise by "
            "the referencing event and strictly smaller in at least one "
            "component",
            event_ids=related,
            details={"violations": violations},
        )


# ---------------------------------------------------------------------------
# Causal layers and concurrency
# ---------------------------------------------------------------------------

def _build_layers(events, by_key):
    """Earliest-executable layering: layer(e) = 1 + max(layer of causal
    predecessors), sources sit in layer 0.  Processing in clock-sum order is
    a valid topological order because accepted edges strictly decrease the
    component sum."""
    ordered = sorted(
        events,
        key=lambda e: (sum(e.clock.values()), e.participant, e.seq, e.id),
    )
    layer_of = {}
    for e in ordered:
        best = -1
        if e.seq > 1:
            best = layer_of[(e.participant, e.seq - 1)]
        for q, v in e.clock.items():
            if q != e.participant and v > 0:
                pred = layer_of[(q, v)]
                if pred > best:
                    best = pred
        layer_of[(e.participant, e.seq)] = best + 1

    buckets = {}
    for e in events:
        buckets.setdefault(layer_of[(e.participant, e.seq)], []).append(e)
    layers = []
    for depth in sorted(buckets):
        members = sorted(buckets[depth],
                         key=lambda e: (e.participant, e.seq, e.id))
        layers.append({"index": depth,
                       "event_ids": [e.id for e in members]})
    return layers


def _count_concurrent_pairs(events, participants, counts):
    """Pairs neither of which happens-before the other.

    In an accepted batch ``a`` happens-before ``b`` iff
    ``a.seq <= b.clock[a.participant]``; comparable pairs are therefore
    countable per event in O(participants)."""
    total = len(events) * (len(events) - 1) // 2
    comparable = 0
    for e in events:
        seen = sum(min(e.clock[q], counts[q]) for q in participants)
        comparable += seen - 1  # exclude the event itself
    return total - comparable


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def audit_batch(payload):
    """Validate a batch payload and return its deterministic causal plan.

    Raises AuditError (status 422) on any rule violation; returns the
    ordered-layer response document on success."""
    if not isinstance(payload, dict):
        raise AuditError(
            "INVALID_SCHEMA",
            "request body must be a JSON object with 'participants' and "
            "'events'",
        )
    missing = [k for k in ("participants", "events") if k not in payload]
    if missing:
        raise AuditError(
            "INVALID_SCHEMA",
            "missing required field(s): %s" % ", ".join(missing),
        )

    participants = _check_participants(payload)
    participant_set = set(participants)
    raw_events = _check_event_schema(payload)
    events = _parse_events(raw_events, participant_set)
    _check_clocks(events, participant_set)

    grouped = _group_by_participant(events, participants)
    _check_sequences(grouped, participants)
    _check_regression(grouped, participants)

    counts = {p: len(grouped[p]) for p in participants}
    _check_references_exist(events, counts)

    by_key = {(e.participant, e.seq): e for e in events}
    edges = _reference_edges(events)
    _check_acyclic(events, edges, by_key)
    _check_domination(events, by_key)

    layers = _build_layers(events, by_key)
    merge_order = [eid for layer in layers for eid in layer["event_ids"]]
    return {
        "event_count": len(events),
        "layer_count": len(layers),
        "layers": layers,
        "merge_order": merge_order,
        "concurrent_pairs": _count_concurrent_pairs(
            events, participants, counts),
    }
