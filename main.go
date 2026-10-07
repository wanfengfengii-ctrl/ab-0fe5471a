package main

import (
	"flag"
	"fmt"
	"net/http"
	"os"
	"time"
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "probe the local service /healthz endpoint and exit 0 when healthy")
	flag.Parse()

	port := os.Getenv("APP_PORT")
	if port == "" {
		port = "8080"
	}

	if *healthcheck {
		os.Exit(probeHealth(port))
	}

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           newMux(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	fmt.Printf("vector-clock audit service listening on :%s\n", port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "server error:", err)
		os.Exit(1)
	}
}

func newMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", healthHandler)
	mux.HandleFunc("/api/events/audit", auditHandler)
	return mux
}

// probeHealth is used by the Docker HEALTHCHECK: it exits 0 only when the
// local service answers /healthz with 200.
func probeHealth(port string) int {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://127.0.0.1:" + port + "/healthz")
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusOK {
		return 0
	}
	return 1
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, errf(http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}
