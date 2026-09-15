package controller

import (
	"context"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/log"
)

// timed runs fn while logging the elapsed wall-clock duration on every exit
// path — success, error, and context cancellation. The previous logging gap
// was that phase failures returned early with no elapsed at all, so a hung
// step was indistinguishable from a fast failure. ctx must carry a logger
// (controller-runtime injects one into every Reconcile ctx; team-scoped
// values are added in TeamReconciler.Reconcile), and any additional context
// (member name, role, ...) is picked up from the same logger.
func timed(ctx context.Context, phase string, fn func() error) error {
	_, err := timedValue(ctx, phase, func() (struct{}, error) {
		return struct{}{}, fn()
	})
	return err
}

// timedValue is timed for functions that return a value. Used to time
// backend calls (Status/Create/Delete) whose results are needed by the caller.
func timedValue[T any](ctx context.Context, phase string, fn func() (T, error)) (T, error) {
	logger := log.FromContext(ctx)
	start := time.Now()
	v, err := fn()
	elapsed := time.Since(start).Truncate(time.Millisecond)
	if ctx.Err() != nil {
		logger.Error(ctx.Err(), "timed-call cancelled", "phase", phase, "elapsed", elapsed.String())
	} else if err != nil {
		logger.Error(err, "timed-call failed", "phase", phase, "elapsed", elapsed.String())
	} else {
		logger.Info("timed-call", "phase", phase, "elapsed", elapsed.String())
	}
	return v, err
}
