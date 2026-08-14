//go:build !pprof

package main

import "context"

// maybeStartPprof is a no-op in default builds: no pprof code is compiled,
// no extra port is opened, and the debugging surface stays zero.
func maybeStartPprof(context.Context) {}
