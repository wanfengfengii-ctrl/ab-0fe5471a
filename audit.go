package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
)

// maxBodyBytes is the hard request-body limit: 1 MiB.
const maxBodyBytes = 1 << 20

// Stable machine-readable error codes.
const (
	codePayloadTooLarge      = "PAYLOAD_TOO_LARGE"
	codeMalformedJSON        = "MALFORMED_JSON"
	codeInvalidRequest       = "INVALID_REQUEST"
	codeInvalidParticipant   = "INVALID_PARTICIPANT"
	codeParticipantCount     = "PARTICIPANT_COUNT_OUT_OF_RANGE"
	codeDuplicateParticipant = "DUPLICATE_PARTICIPANT"
	codeInvalidEvent         = "INVALID_EVENT"
	codeEventCount           = "EVENT_COUNT_OUT_OF_RANGE"
	codeUnknownParticipant   = "UNKNOWN_PARTICIPANT"
	codeDuplicateEventID     = "DUPLICATE_EVENT_ID"
	codeInvalidSequence      = "INVALID_SEQUENCE"
	codeSequenceGap          = "SEQUENCE_GAP"
	codeInvalidClock         = "INVALID_CLOCK"
	codeClockOwnMismatch     = "CLOCK_OWN_MISMATCH"
	codeClockRegression      = "CLOCK_REGRESSION"
	codeFutureReference      = "FUTURE_REFERENCE"
	codeCycleDetected        = "CYCLE_DETECTED"
	codeCausalityViolation   = "CAUSALITY_VIOLATION"
)

// apiError is a rejection with a stable code and the related event ids.
type apiError struct {
	Status   int
	Code     string
	Message  string
	EventIDs []string
}

func (e *apiError) Error() string { return e.Code + ": " + e.Message }

func errf(status int, code, format string, args ...interface{}) *apiError {
	return &apiError{
		Status:   status,
		Code:     code,
		Message:  fmt.Sprintf(format, args...),
		EventIDs: []string{},
	}
}

// withIDs attaches the de-duplicated, sorted related event ids so the
// response is identical no matter how the input batch was ordered.
func (e *apiError) withIDs(ids []string) *apiError {
	seen := make(map[string]bool, len(ids))
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	sort.Strings(out)
	e.EventIDs = out
	return e
}

// event is one validated record of the batch.
type event struct {
	ID          string
	Participant string
	Seq         int64
	Clock       map[string]int64
}

func auditHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeError(w, errf(http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use POST"))
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeError(w, errf(http.StatusRequestEntityTooLarge, codePayloadTooLarge,
			"request body exceeds the 1 MiB limit"))
		return
	}
	participants, events, aerr := parseAndValidate(body)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	if aerr := validateCausality(participants, events); aerr != nil {
		writeError(w, aerr)
		return
	}
	result, aerr := analyze(participants, events)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// parseAndValidate decodes the body and runs every structural check, in a
// fixed order so the reported code never depends on the input ordering.
func parseAndValidate(body []byte) ([]string, []event, *apiError) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var top interface{}
	if err := dec.Decode(&top); err != nil {
		return nil, nil, errf(http.StatusBadRequest, codeMalformedJSON, "request body is not valid JSON")
	}
	var extra interface{}
	if err := dec.Decode(&extra); err != io.EOF {
		return nil, nil, errf(http.StatusBadRequest, codeMalformedJSON, "unexpected data after the JSON document")
	}
	obj, ok := top.(map[string]interface{})
	if !ok {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidRequest, "request body must be a JSON object")
	}

	// ---- participants ----
	rawP, present := obj["participants"]
	if !present {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidParticipant, "participants is required")
	}
	pList, ok := rawP.([]interface{})
	if !ok {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidParticipant, "participants must be an array of unique non-empty names")
	}
	participants := make([]string, 0, len(pList))
	seenP := make(map[string]bool, len(pList))
	dupP := make(map[string]bool)
	badName := false
	for _, rp := range pList {
		name, ok := rp.(string)
		if !ok || name == "" {
			badName = true
			continue
		}
		if seenP[name] {
			dupP[name] = true
		}
		seenP[name] = true
		participants = append(participants, name)
	}
	if badName {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidParticipant, "every participant must be a non-empty string")
	}
	if len(participants) < 2 || len(participants) > 32 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeParticipantCount,
			"participants must declare between 2 and 32 unique parties, got %d", len(participants))
	}
	if len(dupP) > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeDuplicateParticipant,
			"duplicate participant names: %s", strings.Join(sortedKeys(dupP), ", "))
	}

	// ---- events ----
	rawE, present := obj["events"]
	if !present {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeEventCount, "events is required and must contain between 1 and 2000 events")
	}
	eList, ok := rawE.([]interface{})
	if !ok {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidEvent, "events must be an array")
	}
	if len(eList) < 1 || len(eList) > 2000 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeEventCount,
			"events must contain between 1 and 2000 events, got %d", len(eList))
	}

	shapeBad := 0
	var shapeIDs, unknownIDs, seqIDs, clockIDs, allIDs []string
	events := make([]event, 0, len(eList))
	for _, re := range eList {
		eo, ok := re.(map[string]interface{})
		if !ok {
			shapeBad++
			continue
		}
		id, _ := eo["id"].(string)
		part, _ := eo["participant"].(string)
		rawSeq, hasSeq := eo["seq"]
		rawClock, hasClock := eo["clock"]
		if id != "" {
			allIDs = append(allIDs, id)
		}
		if id == "" || part == "" || !hasSeq || !hasClock {
			shapeBad++
			if id != "" {
				shapeIDs = append(shapeIDs, id)
			}
			continue
		}
		if !seenP[part] {
			unknownIDs = append(unknownIDs, id)
			continue
		}
		seq, ok := asNonNegInt(rawSeq)
		if !ok || seq < 1 {
			seqIDs = append(seqIDs, id)
			continue
		}
		clockObj, ok := rawClock.(map[string]interface{})
		if !ok {
			clockIDs = append(clockIDs, id)
			continue
		}
		clock, ok := parseClock(clockObj, participants)
		if !ok {
			clockIDs = append(clockIDs, id)
			continue
		}
		events = append(events, event{ID: id, Participant: part, Seq: seq, Clock: clock})
	}
	if shapeBad > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidEvent,
			"%d event(s) are not objects with a non-empty string id and participant plus seq and clock fields", shapeBad).withIDs(shapeIDs)
	}
	if len(unknownIDs) > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeUnknownParticipant,
			"events reference participants that are not declared").withIDs(unknownIDs)
	}
	if dups := duplicates(allIDs); len(dups) > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeDuplicateEventID,
			"event ids must be unique").withIDs(dups)
	}
	if len(seqIDs) > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidSequence,
			"seq must be a positive integer starting from 1").withIDs(seqIDs)
	}
	if len(clockIDs) > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeInvalidClock,
			"clock must cover exactly the declared participants with non-negative integer values").withIDs(clockIDs)
	}

	// ---- per-participant sequence continuity: seqs must be exactly 1..n ----
	byPart := make(map[string][]event, len(participants))
	for _, e := range events {
		byPart[e.Participant] = append(byPart[e.Participant], e)
	}
	var gapIDs []string
	var gapParts []string
	for _, p := range participants {
		evs := byPart[p]
		seenSeq := make(map[int64]bool, len(evs))
		ok := true
		for _, e := range evs {
			if e.Seq > int64(len(evs)) || seenSeq[e.Seq] {
				ok = false
			}
			seenSeq[e.Seq] = true
		}
		if !ok {
			gapParts = append(gapParts, p)
			for _, e := range evs {
				gapIDs = append(gapIDs, e.ID)
			}
		}
	}
	if len(gapParts) > 0 {
		return nil, nil, errf(http.StatusUnprocessableEntity, codeSequenceGap,
			"sequence numbers must be consecutive starting from 1 for participant(s): %s",
			strings.Join(gapParts, ", ")).withIDs(gapIDs)
	}
	return participants, events, nil
}

// validateCausality runs the vector-clock consistency checks. After
// parseAndValidate every participant owns exactly the seqs 1..n.
func validateCausality(participants []string, events []event) *apiError {
	// An event's own clock component must equal its sequence number.
	var mism []string
	for _, e := range events {
		if e.Clock[e.Participant] != e.Seq {
			mism = append(mism, e.ID)
		}
	}
	if len(mism) > 0 {
		return errf(http.StatusUnprocessableEntity, codeClockOwnMismatch,
			"an event's own clock component must equal its sequence number").withIDs(mism)
	}

	byPS := indexEvents(events)

	// Successor events of one participant must not regress in any component.
	var regIDs []string
	for _, p := range participants {
		m := byPS[p]
		for s := int64(2); s <= int64(len(m)); s++ {
			prev, cur := events[m[s-1]], events[m[s]]
			for _, name := range participants {
				if cur.Clock[name] < prev.Clock[name] {
					regIDs = append(regIDs, prev.ID, cur.ID)
					break
				}
			}
		}
	}
	if len(regIDs) > 0 {
		return errf(http.StatusUnprocessableEntity, codeClockRegression,
			"the vector clock of a participant's successor event regresses").withIDs(regIDs)
	}

	// Every claimed sighting of another party's k-th event must exist.
	deps, refs := buildDeps(participants, events, byPS)
	var futIDs []string
	for _, e := range events {
		for _, p := range participants {
			if p == e.Participant {
				continue
			}
			k := e.Clock[p]
			if k == 0 {
				continue
			}
			if _, ok := byPS[p][k]; !ok {
				futIDs = append(futIDs, e.ID)
			}
		}
	}
	if len(futIDs) > 0 {
		return errf(http.StatusUnprocessableEntity, codeFutureReference,
			"events reference events of other participants that do not exist in the batch").withIDs(futIDs)
	}

	// The declared dependencies must be acyclic.
	if cyc := findCycleNodes(deps); len(cyc) > 0 {
		ids := make([]string, 0, len(cyc))
		for _, idx := range cyc {
			ids = append(ids, events[idx].ID)
		}
		return errf(http.StatusUnprocessableEntity, codeCycleDetected,
			"declared dependencies form a cycle").withIDs(ids)
	}

	// A referenced event's clock must be covered component-wise by the
	// referencing event, with at least one component strictly smaller.
	var violIDs []string
	for _, r := range refs {
		f, e := events[r[0]], events[r[1]]
		covered, strict := true, false
		for _, p := range participants {
			if f.Clock[p] > e.Clock[p] {
				covered = false
				break
			}
			if f.Clock[p] < e.Clock[p] {
				strict = true
			}
		}
		if !covered || !strict {
			violIDs = append(violIDs, f.ID, e.ID)
		}
	}
	if len(violIDs) > 0 {
		return errf(http.StatusUnprocessableEntity, codeCausalityViolation,
			"a referenced event's clock is not strictly dominated by the referencing event's clock").withIDs(violIDs)
	}
	return nil
}

type layerView struct {
	Index    int      `json:"index"`
	EventIDs []string `json:"eventIds"`
}

type auditResult struct {
	EventCount      int         `json:"eventCount"`
	LayerCount      int         `json:"layerCount"`
	Layers          []layerView `json:"layers"`
	Order           []string    `json:"order"`
	ConcurrentPairs int64       `json:"concurrentPairs"`
}

// analyze layers a valid batch by earliest possible execution and counts the
// causally unordered (concurrent) event pairs.
func analyze(participants []string, events []event) (auditResult, *apiError) {
	n := len(events)
	deps, _ := buildDeps(participants, events, indexEvents(events))

	// Kahn's algorithm level by level: a node's level is the earliest slot in
	// which everything it causally depends on has already been executed.
	indeg := make([]int, n)
	succ := make([][]int, n)
	for to, ds := range deps {
		indeg[to] = len(ds)
		for _, from := range ds {
			succ[from] = append(succ[from], to)
		}
	}
	var layers [][]int
	frontier := make([]int, 0, n)
	for i := 0; i < n; i++ {
		if indeg[i] == 0 {
			frontier = append(frontier, i)
		}
	}
	processed := 0
	for len(frontier) > 0 {
		layers = append(layers, frontier)
		var next []int
		for _, u := range frontier {
			processed++
			for _, v := range succ[u] {
				indeg[v]--
				if indeg[v] == 0 {
					next = append(next, v)
				}
			}
		}
		frontier = next
	}
	if processed != n {
		// Unreachable once validateCausality has passed; kept as a safety net.
		var ids []string
		for i := 0; i < n; i++ {
			if indeg[i] > 0 {
				ids = append(ids, events[i].ID)
			}
		}
		return auditResult{}, errf(http.StatusUnprocessableEntity, codeCycleDetected,
			"declared dependencies form a cycle").withIDs(ids)
	}

	res := auditResult{EventCount: n, LayerCount: len(layers)}
	res.Layers = make([]layerView, 0, len(layers))
	res.Order = make([]string, 0, n)
	for li, members := range layers {
		sort.Slice(members, func(a, b int) bool {
			ea, eb := events[members[a]], events[members[b]]
			if ea.Participant != eb.Participant {
				return ea.Participant < eb.Participant
			}
			if ea.Seq != eb.Seq {
				return ea.Seq < eb.Seq
			}
			return ea.ID < eb.ID
		})
		ids := make([]string, len(members))
		for i, m := range members {
			ids[i] = events[m].ID
		}
		res.Layers = append(res.Layers, layerView{Index: li, EventIDs: ids})
		res.Order = append(res.Order, ids...)
	}

	// Concurrent pairs: neither clock dominates the other component-wise.
	var concurrent int64
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			iLeJ, jLeI := true, true
			for _, p := range participants {
				a, b := events[i].Clock[p], events[j].Clock[p]
				if a > b {
					iLeJ = false
				}
				if a < b {
					jLeI = false
				}
				if !iLeJ && !jLeI {
					break
				}
			}
			if !iLeJ && !jLeI {
				concurrent++
			}
		}
	}
	res.ConcurrentPairs = concurrent
	return res, nil
}

// indexEvents maps (participant, seq) to the event's position in events.
func indexEvents(events []event) map[string]map[int64]int {
	byPS := make(map[string]map[int64]int)
	for i, e := range events {
		m := byPS[e.Participant]
		if m == nil {
			m = make(map[int64]int)
			byPS[e.Participant] = m
		}
		m[e.Seq] = i
	}
	return byPS
}

// buildDeps derives the causal dependency graph: program order (each event
// depends on its participant's predecessor) plus every sighting of another
// party's k-th event. deps[to] lists the events that must run before to;
// refs holds the (referenced, referencing) pairs for domination checks.
func buildDeps(participants []string, events []event, byPS map[string]map[int64]int) (deps [][]int, refs [][2]int) {
	deps = make([][]int, len(events))
	for i, e := range events {
		if e.Seq > 1 {
			deps[i] = append(deps[i], byPS[e.Participant][e.Seq-1])
		}
		for _, p := range participants {
			if p == e.Participant {
				continue
			}
			k := e.Clock[p]
			if k == 0 {
				continue
			}
			j, ok := byPS[p][k]
			if !ok {
				continue // future reference; reported during validation
			}
			deps[i] = append(deps[i], j)
			refs = append(refs, [2]int{j, i})
		}
	}
	return deps, refs
}

// findCycleNodes returns the nodes that belong to a cycle (Tarjan SCC).
func findCycleNodes(deps [][]int) []int {
	n := len(deps)
	index := make([]int, n)
	low := make([]int, n)
	onStack := make([]bool, n)
	inCycle := make([]bool, n)
	for i := range index {
		index[i] = -1
	}
	var stack []int
	counter := 0

	var strongconnect func(v int)
	strongconnect = func(v int) {
		index[v], low[v] = counter, counter
		counter++
		stack = append(stack, v)
		onStack[v] = true
		for _, w := range deps[v] {
			if index[w] == -1 {
				strongconnect(w)
				if low[w] < low[v] {
					low[v] = low[w]
				}
			} else if onStack[w] && index[w] < low[v] {
				low[v] = index[w]
			}
		}
		if low[v] == index[v] {
			var comp []int
			for {
				w := stack[len(stack)-1]
				stack = stack[:len(stack)-1]
				onStack[w] = false
				comp = append(comp, w)
				if w == v {
					break
				}
			}
			if len(comp) > 1 {
				for _, w := range comp {
					inCycle[w] = true
				}
			} else {
				for _, w := range deps[comp[0]] {
					if w == comp[0] {
						inCycle[comp[0]] = true
					}
				}
			}
		}
	}
	for v := 0; v < n; v++ {
		if index[v] == -1 {
			strongconnect(v)
		}
	}
	var out []int
	for i, c := range inCycle {
		if c {
			out = append(out, i)
		}
	}
	return out
}

// asNonNegInt accepts only non-negative integer JSON literals that fit int64.
func asNonNegInt(v interface{}) (int64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	s := n.String()
	if s == "" {
		return 0, false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0, false
		}
	}
	val, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, false
	}
	return val, true
}

// parseClock requires exactly the declared participants, each mapped to a
// non-negative integer.
func parseClock(obj map[string]interface{}, participants []string) (map[string]int64, bool) {
	if len(obj) != len(participants) {
		return nil, false
	}
	clock := make(map[string]int64, len(participants))
	for _, p := range participants {
		raw, ok := obj[p]
		if !ok {
			return nil, false
		}
		v, ok := asNonNegInt(raw)
		if !ok {
			return nil, false
		}
		clock[p] = v
	}
	return clock, true
}

func duplicates(ids []string) []string {
	counts := make(map[string]int, len(ids))
	for _, id := range ids {
		counts[id]++
	}
	var out []string
	for id, c := range counts {
		if c > 1 {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

type errorBody struct {
	Code     string   `json:"code"`
	Message  string   `json:"message"`
	EventIDs []string `json:"eventIds"`
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, e *apiError) {
	writeJSON(w, e.Status, map[string]interface{}{
		"error": errorBody{Code: e.Code, Message: e.Message, EventIDs: e.EventIDs},
	})
}
