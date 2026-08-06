//go:build pprof

package main

import (
	"context"
	"fmt"
	"net/http"
	_ "net/http/pprof"
	"os"
	"runtime"
	"time"
)

// maybeStartPprof starts the net/http/pprof server. Compiled only with
// `-tags pprof` (see hiclaw-controller/Dockerfile ENABLE_PPROF build arg);
// the default build gets the no-op stub in pprof_stub.go. The listen
// address is overridable via HICLAW_PPROF_ADDR (default 0.0.0.0:6060,
// reachable via `kubectl port-forward` or `docker exec curl`).
func maybeStartPprof(ctx context.Context) {
	// net/http/pprof records no block/mutex samples by default (rates are 0);
	// enable them so the debugging image yields meaningful block/mutex
	// profiles. Sampling overhead is acceptable in a debug build only.
	runtime.SetBlockProfileRate(1)
	runtime.SetMutexProfileFraction(1)

	addr := os.Getenv("HICLAW_PPROF_ADDR")
	if addr == "" {
		addr = "0.0.0.0:6060"
	}
	srv := &http.Server{Addr: addr, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Fprintf(os.Stderr, "pprof server: %v\n", err)
		}
	}()
}
