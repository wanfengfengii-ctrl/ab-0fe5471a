package main

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
)

func doRequest(t *testing.T, method, path, body string) (int, string) {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	rec := httptest.NewRecorder()
	newMux().ServeHTTP(rec, req)
	return rec.Code, rec.Body.String()
}

const validBatch = `{
  "participants": ["alpha", "beta"],
  "events": [
    {"id":"a1","participant":"alpha","seq":1,"clock":{"alpha":1,"beta":0}},
    {"id":"b1","participant":"beta","seq":1,"clock":{"alpha":0,"beta":1}},
    {"id":"a2","participant":"alpha","seq":2,"clock":{"alpha":2,"beta":1}},
    {"id":"b2","participant":"beta","seq":2,"clock":{"alpha":2,"beta":2}},
    {"id":"a3","participant":"alpha","seq":3,"clock":{"alpha":3,"beta":2}}
  ]
}`

// Same batch, events reversed and participants swapped: the response must be
// byte-identical.
const validBatchShuffled = `{
  "participants": ["beta", "alpha"],
  "events": [
    {"id":"a3","participant":"alpha","seq":3,"clock":{"beta":2,"alpha":3}},
    {"id":"b2","participant":"beta","seq":2,"clock":{"beta":2,"alpha":2}},
    {"id":"a2","participant":"alpha","seq":2,"clock":{"beta":1,"alpha":2}},
    {"id":"b1","participant":"beta","seq":1,"clock":{"beta":1,"alpha":0}},
    {"id":"a1","participant":"alpha","seq":1,"clock":{"beta":0,"alpha":1}}
  ]
}`

func TestValidBatchExactResponse(t *testing.T) {
	code, body := doRequest(t, http.MethodPost, "/api/events/audit", validBatch)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, body)
	}
	want := `{"eventCount":5,"layerCount":4,"layers":[` +
		`{"index":0,"eventIds":["a1","b1"]},` +
		`{"index":1,"eventIds":["a2"]},` +
		`{"index":2,"eventIds":["b2"]},` +
		`{"index":3,"eventIds":["a3"]}],` +
		`"order":["a1","b1","a2","b2","a3"],"concurrentPairs":1}` + "\n"
	if body != want {
		t.Fatalf("body mismatch\ngot:  %s\nwant: %s", body, want)
	}
}

func TestInputPermutationDoesNotChangeResult(t *testing.T) {
	code1, body1 := doRequest(t, http.MethodPost, "/api/events/audit", validBatch)
	code2, body2 := doRequest(t, http.MethodPost, "/api/events/audit", validBatchShuffled)
	if code1 != http.StatusOK || code2 != http.StatusOK {
		t.Fatalf("statuses %d, %d", code1, code2)
	}
	if body1 != body2 {
		t.Fatalf("permuted input changed the result:\n%s\nvs\n%s", body1, body2)
	}
}

func TestHealthz(t *testing.T) {
	code, body := doRequest(t, http.MethodGet, "/healthz", "")
	if code != http.StatusOK || !strings.Contains(body, `"status":"ok"`) {
		t.Fatalf("healthz = %d %s", code, body)
	}
}

type errCase struct {
	name       string
	body       string
	wantStatus int
	wantCode   string
	wantIDs    []string
}

func TestRejections(t *testing.T) {
	cases := []errCase{
		{
			name:       "one participant",
			body:       `{"participants":["a"],"events":[{"id":"e1","participant":"a","seq":1,"clock":{"a":1}}]}`,
			wantStatus: 422, wantCode: codeParticipantCount,
		},
		{
			name: "thirty-three participants",
			body: func() string {
				ps := make([]string, 33)
				for i := range ps {
					ps[i] = fmt.Sprintf(`"p%d"`, i)
				}
				return `{"participants":[` + strings.Join(ps, ",") + `],"events":[]}`
			}(),
			wantStatus: 422, wantCode: codeParticipantCount,
		},
		{
			name:       "duplicate participant",
			body:       `{"participants":["a","b","a"],"events":[]}`,
			wantStatus: 422, wantCode: codeDuplicateParticipant,
		},
		{
			name:       "empty participant name",
			body:       `{"participants":["a",""],"events":[]}`,
			wantStatus: 422, wantCode: codeInvalidParticipant,
		},
		{
			name:       "participants not an array",
			body:       `{"participants":"ab","events":[]}`,
			wantStatus: 422, wantCode: codeInvalidParticipant,
		},
		{
			name:       "zero events",
			body:       `{"participants":["a","b"],"events":[]}`,
			wantStatus: 422, wantCode: codeEventCount,
		},
		{
			name: "two thousand one events",
			body: func() string {
				var b strings.Builder
				b.WriteString(`{"participants":["a","b"],"events":[`)
				for i := 1; i <= 2001; i++ {
					if i > 1 {
						b.WriteString(",")
					}
					fmt.Fprintf(&b, `{"id":"e%d","participant":"a","seq":%d,"clock":{"a":%d,"b":0}}`, i, i, i)
				}
				b.WriteString(`]}`)
				return b.String()
			}(),
			wantStatus: 422, wantCode: codeEventCount,
		},
		{
			name:       "event not an object",
			body:       `{"participants":["a","b"],"events":[42]}`,
			wantStatus: 422, wantCode: codeInvalidEvent,
		},
		{
			name:       "event missing clock",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1}]}`,
			wantStatus: 422, wantCode: codeInvalidEvent, wantIDs: []string{"e1"},
		},
		{
			name:       "unknown participant",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"zzz","seq":1,"clock":{"a":0,"b":0}}]}`,
			wantStatus: 422, wantCode: codeUnknownParticipant, wantIDs: []string{"e1"},
		},
		{
			name: "duplicate event id",
			body: `{"participants":["a","b"],"events":[
				{"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":0}},
				{"id":"e1","participant":"b","seq":1,"clock":{"a":0,"b":1}}]}`,
			wantStatus: 422, wantCode: codeDuplicateEventID, wantIDs: []string{"e1"},
		},
		{
			name:       "seq zero",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":0,"clock":{"a":0,"b":0}}]}`,
			wantStatus: 422, wantCode: codeInvalidSequence, wantIDs: []string{"e1"},
		},
		{
			name:       "seq negative",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":-2,"clock":{"a":0,"b":0}}]}`,
			wantStatus: 422, wantCode: codeInvalidSequence, wantIDs: []string{"e1"},
		},
		{
			name:       "seq fractional",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1.5,"clock":{"a":1,"b":0}}]}`,
			wantStatus: 422, wantCode: codeInvalidSequence, wantIDs: []string{"e1"},
		},
		{
			name: "sequence gap",
			body: `{"participants":["a","b"],"events":[
				{"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":0}},
				{"id":"e3","participant":"a","seq":3,"clock":{"a":3,"b":0}}]}`,
			wantStatus: 422, wantCode: codeSequenceGap, wantIDs: []string{"e1", "e3"},
		},
		{
			name: "duplicate sequence",
			body: `{"participants":["a","b"],"events":[
				{"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":0}},
				{"id":"e2","participant":"a","seq":1,"clock":{"a":1,"b":0}}]}`,
			wantStatus: 422, wantCode: codeSequenceGap, wantIDs: []string{"e1", "e2"},
		},
		{
			name:       "clock missing participant",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1,"clock":{"a":1}}]}`,
			wantStatus: 422, wantCode: codeInvalidClock, wantIDs: []string{"e1"},
		},
		{
			name:       "clock extra participant",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":0,"c":0}}]}`,
			wantStatus: 422, wantCode: codeInvalidClock, wantIDs: []string{"e1"},
		},
		{
			name:       "clock negative value",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":-1}}]}`,
			wantStatus: 422, wantCode: codeInvalidClock, wantIDs: []string{"e1"},
		},
		{
			name:       "clock fractional value",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":0.5}}]}`,
			wantStatus: 422, wantCode: codeInvalidClock, wantIDs: []string{"e1"},
		},
		{
			name:       "own component mismatch",
			body:       `{"participants":["a","b"],"events":[{"id":"e1","participant":"a","seq":1,"clock":{"a":2,"b":0}}]}`,
			wantStatus: 422, wantCode: codeClockOwnMismatch, wantIDs: []string{"e1"},
		},
		{
			name: "clock regression",
			body: `{"participants":["a","b"],"events":[
				{"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":1}},
				{"id":"b1","participant":"b","seq":1,"clock":{"a":0,"b":1}},
				{"id":"a2","participant":"a","seq":2,"clock":{"a":2,"b":0}}]}`,
			wantStatus: 422, wantCode: codeClockRegression, wantIDs: []string{"a1", "a2"},
		},
		{
			name: "future reference",
			body: `{"participants":["a","b"],"events":[
				{"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":5}},
				{"id":"b1","participant":"b","seq":1,"clock":{"a":0,"b":1}}]}`,
			wantStatus: 422, wantCode: codeFutureReference, wantIDs: []string{"a1"},
		},
		{
			name: "mutual reference cycle",
			body: `{"participants":["a","b"],"events":[
				{"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":1}},
				{"id":"b1","participant":"b","seq":1,"clock":{"a":1,"b":1}}]}`,
			wantStatus: 422, wantCode: codeCycleDetected, wantIDs: []string{"a1", "b1"},
		},
		{
			name: "three event reference cycle",
			body: `{"participants":["a","b","c"],"events":[
				{"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":1,"c":0}},
				{"id":"b1","participant":"b","seq":1,"clock":{"a":0,"b":1,"c":1}},
				{"id":"c1","participant":"c","seq":1,"clock":{"a":1,"b":0,"c":1}}]}`,
			wantStatus: 422, wantCode: codeCycleDetected, wantIDs: []string{"a1", "b1", "c1"},
		},
		{
			name: "causality violation without cycle",
			body: `{"participants":["p","q","r"],"events":[
				{"id":"e1","participant":"p","seq":1,"clock":{"p":1,"q":1,"r":0}},
				{"id":"f1","participant":"q","seq":1,"clock":{"p":0,"q":1,"r":1}},
				{"id":"r1","participant":"r","seq":1,"clock":{"p":0,"q":0,"r":1}}]}`,
			wantStatus: 422, wantCode: codeCausalityViolation, wantIDs: []string{"e1", "f1"},
		},
		{
			name:       "malformed json",
			body:       `{"participants": [`,
			wantStatus: 400, wantCode: codeMalformedJSON,
		},
		{
			name:       "trailing data",
			body:       `{"participants":["a","b"],"events":[]} {}`,
			wantStatus: 400, wantCode: codeMalformedJSON,
		},
		{
			name:       "top level not an object",
			body:       `[1,2,3]`,
			wantStatus: 422, wantCode: codeInvalidRequest,
		},
		{
			name:       "body over one MiB",
			body:       strings.Repeat(" ", 1<<20+1),
			wantStatus: 413, wantCode: codePayloadTooLarge,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, body := doRequest(t, http.MethodPost, "/api/events/audit", tc.body)
			if code != tc.wantStatus {
				t.Fatalf("status = %d, want %d, body = %s", code, tc.wantStatus, body)
			}
			var parsed struct {
				Error errorBody `json:"error"`
			}
			if err := json.Unmarshal([]byte(body), &parsed); err != nil {
				t.Fatalf("error response is not JSON: %s", body)
			}
			if parsed.Error.Code != tc.wantCode {
				t.Fatalf("code = %s, want %s (body %s)", parsed.Error.Code, tc.wantCode, body)
			}
			if tc.wantIDs != nil {
				got := parsed.Error.EventIDs
				if !equalStrings(got, tc.wantIDs) {
					t.Fatalf("eventIds = %v, want %v", got, tc.wantIDs)
				}
			}
		})
	}
}

func TestMethodNotAllowed(t *testing.T) {
	code, _ := doRequest(t, http.MethodGet, "/api/events/audit", "")
	if code != http.StatusMethodNotAllowed {
		t.Fatalf("GET audit = %d, want 405", code)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// genHistory simulates the standard vector-clock protocol to produce a valid
// batch: local events increment the own component, "receives" merge a peer's
// current clock first.
func genHistory(r *rand.Rand, participants []string, n int) []event {
	cur := make(map[string]map[string]int64, len(participants))
	seq := make(map[string]int64, len(participants))
	for _, p := range participants {
		cur[p] = make(map[string]int64, len(participants))
	}
	evs := make([]event, 0, n)
	for i := 0; i < n; i++ {
		p := participants[r.Intn(len(participants))]
		if r.Intn(2) == 0 {
			q := participants[r.Intn(len(participants))]
			if q != p && seq[q] > 0 {
				for _, name := range participants {
					if cur[q][name] > cur[p][name] {
						cur[p][name] = cur[q][name]
					}
				}
			}
		}
		seq[p]++
		cur[p][p] = seq[p]
		clock := make(map[string]int64, len(participants))
		for _, name := range participants {
			clock[name] = cur[p][name]
		}
		evs = append(evs, event{
			ID:          fmt.Sprintf("e%04d", i),
			Participant: p,
			Seq:         seq[p],
			Clock:       clock,
		})
	}
	return evs
}

func buildBody(evs []event, participants []string) string {
	var b strings.Builder
	b.WriteString(`{"participants":[`)
	for i, p := range participants {
		if i > 0 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `%q`, p)
	}
	b.WriteString(`],"events":[`)
	for i, e := range evs {
		if i > 0 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `{"id":%q,"participant":%q,"seq":%d,"clock":{`, e.ID, e.Participant, e.Seq)
		first := true
		for _, p := range participants {
			if !first {
				b.WriteString(",")
			}
			first = false
			fmt.Fprintf(&b, `%q:%d`, p, e.Clock[p])
		}
		b.WriteString(`}}`)
	}
	b.WriteString(`]}`)
	return b.String()
}

// expectedAnalysis is an independent reimplementation used to cross-check the
// service: layers by longest-path layering, concurrency by pairwise clocks.
func expectedAnalysis(participants []string, evs []event) (layers [][]string, concurrent int64) {
	byPS := indexEvents(evs)
	preds := make([][]int, len(evs))
	for i, e := range evs {
		if e.Seq > 1 {
			preds[i] = append(preds[i], byPS[e.Participant][e.Seq-1])
		}
		for _, p := range participants {
			if p == e.Participant {
				continue
			}
			if k := e.Clock[p]; k > 0 {
				preds[i] = append(preds[i], byPS[p][k])
			}
		}
	}
	depth := make([]int, len(evs))
	memo := make([]bool, len(evs))
	var layerOf func(int) int
	layerOf = func(v int) int {
		if memo[v] {
			return depth[v]
		}
		memo[v] = true
		maxd := -1
		for _, u := range preds[v] {
			if d := layerOf(u); d > maxd {
				maxd = d
			}
		}
		depth[v] = maxd + 1
		return depth[v]
	}
	byLayer := map[int][]event{}
	maxLayer := -1
	for i := range evs {
		d := layerOf(i)
		byLayer[d] = append(byLayer[d], evs[i])
		if d > maxLayer {
			maxLayer = d
		}
	}
	for d := 0; d <= maxLayer; d++ {
		group := byLayer[d]
		sort.Slice(group, func(a, b int) bool {
			if group[a].Participant != group[b].Participant {
				return group[a].Participant < group[b].Participant
			}
			if group[a].Seq != group[b].Seq {
				return group[a].Seq < group[b].Seq
			}
			return group[a].ID < group[b].ID
		})
		ids := make([]string, len(group))
		for i, e := range group {
			ids[i] = e.ID
		}
		layers = append(layers, ids)
	}
	for i := 0; i < len(evs); i++ {
		for j := i + 1; j < len(evs); j++ {
			iLeJ, jLeI := true, true
			for _, p := range participants {
				if evs[i].Clock[p] > evs[j].Clock[p] {
					iLeJ = false
				}
				if evs[i].Clock[p] < evs[j].Clock[p] {
					jLeI = false
				}
			}
			if !iLeJ && !jLeI {
				concurrent++
			}
		}
	}
	return layers, concurrent
}

func TestRandomHistories(t *testing.T) {
	for _, cfg := range []struct {
		seed  int64
		parts int
		n     int
	}{
		{1, 2, 40},
		{2, 5, 200},
		{3, 32, 500},
		{4, 7, 1},
	} {
		participants := make([]string, cfg.parts)
		for i := range participants {
			participants[i] = fmt.Sprintf("p%02d", i)
		}
		r := rand.New(rand.NewSource(cfg.seed))
		evs := genHistory(r, participants, cfg.n)
		body := buildBody(evs, participants)

		code, respBody := doRequest(t, http.MethodPost, "/api/events/audit", body)
		if code != http.StatusOK {
			t.Fatalf("seed %d: status = %d, body = %s", cfg.seed, code, respBody)
		}
		var res auditResult
		if err := json.Unmarshal([]byte(respBody), &res); err != nil {
			t.Fatalf("seed %d: response not JSON: %v", cfg.seed, err)
		}
		wantLayers, wantConcurrent := expectedAnalysis(participants, evs)
		if res.EventCount != cfg.n {
			t.Fatalf("seed %d: eventCount = %d, want %d", cfg.seed, res.EventCount, cfg.n)
		}
		if res.LayerCount != len(wantLayers) {
			t.Fatalf("seed %d: layerCount = %d, want %d", cfg.seed, res.LayerCount, len(wantLayers))
		}
		for i, lv := range res.Layers {
			if lv.Index != i {
				t.Fatalf("seed %d: layer index = %d, want %d", cfg.seed, lv.Index, i)
			}
			if !equalStrings(lv.EventIDs, wantLayers[i]) {
				t.Fatalf("seed %d: layer %d = %v, want %v", cfg.seed, i, lv.EventIDs, wantLayers[i])
			}
		}
		var flat []string
		for _, ids := range wantLayers {
			flat = append(flat, ids...)
		}
		if !equalStrings(res.Order, flat) {
			t.Fatalf("seed %d: order mismatch", cfg.seed)
		}
		if res.ConcurrentPairs != wantConcurrent {
			t.Fatalf("seed %d: concurrentPairs = %d, want %d", cfg.seed, res.ConcurrentPairs, wantConcurrent)
		}

		// Permuting events and participants must not change the response.
		r2 := rand.New(rand.NewSource(cfg.seed + 1000))
		perm := r2.Perm(len(evs))
		shuffled := make([]event, len(evs))
		for i, j := range perm {
			shuffled[i] = evs[j]
		}
		pp := append([]string(nil), participants...)
		r2.Shuffle(len(pp), func(i, j int) { pp[i], pp[j] = pp[j], pp[i] })
		code2, respBody2 := doRequest(t, http.MethodPost, "/api/events/audit", buildBody(shuffled, pp))
		if code2 != http.StatusOK || respBody2 != respBody {
			t.Fatalf("seed %d: permutation changed the result (status %d)", cfg.seed, code2)
		}
	}
}

func TestMaxSizeBatch(t *testing.T) {
	participants := make([]string, 32)
	for i := range participants {
		participants[i] = fmt.Sprintf("p%02d", i)
	}
	r := rand.New(rand.NewSource(42))
	evs := genHistory(r, participants, 2000)
	code, body := doRequest(t, http.MethodPost, "/api/events/audit", buildBody(evs, participants))
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %.500s", code, body)
	}
	var res auditResult
	if err := json.Unmarshal([]byte(body), &res); err != nil {
		t.Fatal(err)
	}
	if res.EventCount != 2000 {
		t.Fatalf("eventCount = %d, want 2000", res.EventCount)
	}
	total := 0
	for _, lv := range res.Layers {
		total += len(lv.EventIDs)
	}
	if total != 2000 || len(res.Order) != 2000 {
		t.Fatalf("layers/order do not cover all events: %d, %d", total, len(res.Order))
	}
}
