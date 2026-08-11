// Package metrics defines hiclaw-specific Prometheus metrics and registers
// them with the controller-runtime metrics registry, which exposes them on
// the metrics endpoint configured by ctrl.Options.Metrics.BindAddress.
//
// controller-runtime already exports generic reconcile metrics (controller_
// runtime_reconcile_total etc.); these hiclaw_* metrics carry the CRD kind
// as a label so we can dashboard per-CRD signals without parsing the
// controller name.
package metrics

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

const subsystem = "hiclaw"

var (
	// ReconcileDuration is a histogram of Reconcile latencies in seconds,
	// partitioned by CRD kind and outcome ("success" or "error").
	ReconcileDuration = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Namespace: subsystem,
			Name:      "reconcile_duration_seconds",
			Help:      "Time spent in a single Reconcile call, partitioned by CRD kind and outcome.",
			Buckets:   prometheus.ExponentialBuckets(0.01, 2, 12),
		},
		[]string{"kind", "result"},
	)

	// ReconcileTotal counts Reconcile invocations, partitioned by CRD kind
	// and outcome. The success counter doubles as a liveness signal — if it
	// stops advancing for a kind that has live CRs, the controller is
	// either deadlocked or starved.
	ReconcileTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Namespace: subsystem,
			Name:      "reconcile_total",
			Help:      "Number of Reconcile calls, partitioned by CRD kind and outcome.",
		},
		[]string{"kind", "result"},
	)

	// ReconcileErrors counts Reconcile invocations that returned a non-nil
	// error. Redundant with ReconcileTotal{result="error"} but kept
	// separate so alerts can be written against a single-label series.
	ReconcileErrors = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Namespace: subsystem,
			Name:      "reconcile_errors_total",
			Help:      "Number of Reconcile calls that returned an error.",
		},
		[]string{"kind"},
	)

	// StorageOpDuration is a histogram of object-storage call latencies in
	// seconds, partitioned by operation ("get"/"put"/"putfile"/"stat"/"list"/…)
	// and driver ("mc" subprocess vs "sdk" minio-go). The get/put percentiles
	// are the direct read on storage endpoint health; a rising tail
	// correlates with the per-member config-phase stalls.
	StorageOpDuration = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Namespace: subsystem,
			Name:      "storage_op_duration_seconds",
			Help:      "Time spent in a single object-storage call, partitioned by operation and driver.",
			Buckets:   prometheus.ExponentialBuckets(0.001, 2, 15), // 1ms → ~16s
		},
		[]string{"op", "driver"},
	)

	// StorageOpErrors counts failed object-storage calls, partitioned by
	// operation, driver, and error class ("timeout"/"network"/"not_found"/
	// "other"). A rising timeout/network series is the "storage endpoint is
	// unstable" alarm — the condition that stalls reconcile passes.
	StorageOpErrors = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Namespace: subsystem,
			Name:      "storage_op_errors_total",
			Help:      "Number of failed object-storage calls, partitioned by operation, driver, and error class.",
		},
		[]string{"op", "driver", "class"},
	)

	// StorageProbeFailures counts storage reachability probe failures that
	// aborted a worker config phase early (DeployWorkerConfig fast-abort).
	StorageProbeFailures = prometheus.NewCounter(
		prometheus.CounterOpts{
			Namespace: subsystem,
			Name:      "storage_probe_failures_total",
			Help:      "Number of storage reachability probe failures that aborted a config phase.",
		},
	)

	// MemberReconcileDuration is a histogram of a single member's full
	// reconcile flow (infra + service-account + config + container + expose)
	// in seconds, partitioned by owning CRD kind ("team"/"worker") and
	// outcome. The team-series average is the per-worker converge cost; it
	// drops when the storage driver or skill push is optimized.
	MemberReconcileDuration = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Namespace: subsystem,
			Name:      "member_reconcile_duration_seconds",
			Help:      "Time spent reconciling one member end-to-end, partitioned by CRD kind and outcome.",
			Buckets:   prometheus.ExponentialBuckets(0.01, 2, 14), // 10ms → ~160s
		},
		[]string{"kind", "result"},
	)
)

func init() {
	metrics.Registry.MustRegister(
		ReconcileDuration, ReconcileTotal, ReconcileErrors,
		StorageOpDuration, StorageOpErrors, StorageProbeFailures,
		MemberReconcileDuration,
	)
}

// Observe records duration and outcome for a single Reconcile call. Intended
// to be invoked from a deferred closure so it sees the final value of the
// named error return.
func Observe(kind string, start time.Time, err error) {
	result := "success"
	if err != nil {
		result = "error"
		ReconcileErrors.WithLabelValues(kind).Inc()
	}
	ReconcileDuration.WithLabelValues(kind, result).Observe(time.Since(start).Seconds())
	ReconcileTotal.WithLabelValues(kind, result).Inc()
}
