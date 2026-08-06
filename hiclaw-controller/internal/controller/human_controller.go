package controller

import (
	"context"
	"time"

	v1beta1 "github.com/hiclaw/hiclaw-controller/api/v1beta1"
	"github.com/hiclaw/hiclaw-controller/internal/metrics"
	"github.com/hiclaw/hiclaw-controller/internal/service"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kerrors "k8s.io/apimachinery/pkg/util/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// maxHumanRetries caps consecutive Infra failures before the Human stops
// auto-requeuing (status.maxRetriesReached=true). Reset with
// kubectl annotate human <name> hiclaw.io/retry="".
const maxHumanRetries = 5

// humanRetryAnnotation re-arms automatic retries after maxHumanRetries.
// Same annotation key as the Team reconciler's hiclaw.io/retry.
const humanRetryAnnotation = "hiclaw.io/retry"

// HumanReconciler reconciles Human resources using Service-layer orchestration.
//
// Unlike Worker/Manager, a Human has no backend container and no gateway
// consumer: the reconciler's entire job is to keep a Matrix user plus a
// set of room memberships in sync with Spec.AccessibleWorkers/Teams and
// (in embedded mode) with humans-registry.json.
type HumanReconciler struct {
	client.Client

	Provisioner service.HumanProvisioner
	Legacy      *service.LegacyCompat // nil in incluster mode
}

func (r *HumanReconciler) Reconcile(ctx context.Context, req reconcile.Request) (retres reconcile.Result, reterr error) {
	start := time.Now()
	defer func() { metrics.Observe("human", start, reterr) }()

	logger := log.FromContext(ctx)
	ctx = log.IntoContext(ctx, logger.WithValues(
		"human", req.NamespacedName.Name,
		"namespace", req.NamespacedName.Namespace,
	))

	var human v1beta1.Human
	if err := r.Get(ctx, req.NamespacedName, &human); err != nil {
		return reconcile.Result{}, client.IgnoreNotFound(err)
	}

	// Human exhausted its automatic retry budget: stop requeuing until an
	// operator re-arms retries with the hiclaw.io/retry annotation. Placed
	// before finalizer handling so a retry-capped Human can still be deleted.
	if human.Status.MaxRetriesReached {
		if human.Annotations[humanRetryAnnotation] == "" {
			return reconcile.Result{}, nil
		}
		delete(human.Annotations, humanRetryAnnotation)
		human.Status.MaxRetriesReached = false
		human.Status.ConsecutiveFailures = 0
		if err := r.Update(ctx, &human); err != nil {
			return reconcile.Result{}, err
		}
		// Status lives behind the status subresource; a plain Update does not
		// persist it. Write the reset counters separately so the Human
		// re-enters the normal reconcile path on the next pass.
		if err := r.Status().Update(ctx, &human); err != nil {
			return reconcile.Result{}, err
		}
	}

	patchBase := client.MergeFrom(human.DeepCopy())

	s := &humanScope{
		human:     &human,
		username:  human.Spec.EffectiveUsername(human.Name),
		patchBase: patchBase,
	}

	// Defer status patch so every phase writes through a single merge-patch
	// at the end of the reconcile loop. We skip the patch when the object
	// is being deleted — the finalizer cleanup path calls r.Update itself
	// and the CR may no longer exist by the time the defer runs.
	defer func() {
		if !human.DeletionTimestamp.IsZero() {
			return
		}

		prevPhase := human.Status.Phase
		human.Status.Phase = computeHumanPhase(&human, reterr)
		if human.Status.Phase != prevPhase {
			now := metav1.Now()
			human.Status.PhaseTransitionTime = &now
		}
		if reterr == nil {
			human.Status.Message = ""
			human.Status.ObservedGeneration = human.Generation
			human.Status.ConsecutiveFailures = 0
		} else {
			human.Status.Message = reterr.Error()
		}

		if err := r.Status().Patch(ctx, &human, patchBase); err != nil {
			logger.Error(err, "failed to patch human status")
			reterr = kerrors.NewAggregate([]error{reterr, err})
		}
	}()

	if !human.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(&human, finalizerName) {
			return r.reconcileHumanDelete(ctx, s)
		}
		return reconcile.Result{}, nil
	}

	if !controllerutil.ContainsFinalizer(&human, finalizerName) {
		controllerutil.AddFinalizer(&human, finalizerName)
		if err := r.Update(ctx, &human); err != nil {
			return reconcile.Result{}, err
		}
	}

	// Failed-Human backoff guard. failHuman patches status (which increments
	// ConsecutiveFailures), so the informer re-enqueues the Human immediately;
	// without this guard the exponential backoff schedule would never apply
	// and a failing Human would hammer the queue out of order. Passes that
	// arrive before the backoff window elapsed are dropped (no error, no
	// requeue) — the original RequeueAfter wakeup re-triggers them later.
	if human.Status.Phase == "Failed" && !human.Status.MaxRetriesReached &&
		human.Status.ConsecutiveFailures > 0 && human.Status.PhaseTransitionTime != nil {
		if time.Since(human.Status.PhaseTransitionTime.Time) < failBackoffFor(human.Status.ConsecutiveFailures) {
			return reconcile.Result{}, nil
		}
	}

	return r.reconcileHumanNormal(ctx, s)
}

// reconcileHumanNormal runs the declarative convergence loop. Phases in
// order: infrastructure (Matrix account), rooms (membership), legacy
// (humans-registry.json). Only infrastructure is fatal; the other two
// phases log errors but never return them, so a transient Matrix hiccup
// on room invite/kick does not block the next reconcile.
func (r *HumanReconciler) reconcileHumanNormal(ctx context.Context, s *humanScope) (reconcile.Result, error) {
	if err := r.reconcileHumanInfra(ctx, s); err != nil {
		return r.failHuman(ctx, s, err.Error())
	}
	r.reconcileHumanRooms(ctx, s)
	r.reconcileHumanLegacy(ctx, s)

	return reconcile.Result{RequeueAfter: reconcileInterval}, nil
}

// failHuman records the Infra failure with an explicit exponential backoff
// (30s → 1m → 2m → ... capped at maxFailBackoff) and stops requeuing entirely
// after maxHumanRetries consecutive failures. Returns Result-only (nil error)
// so the workqueue rate limiter does not additionally requeue the Human with
// its own unpredictable backoff on top of the intended RequeueAfter — the
// D-02 double-requeue pattern. Status writes happen through the Reconcile
// defer, not here.
func (r *HumanReconciler) failHuman(ctx context.Context, s *humanScope, msg string) (reconcile.Result, error) {
	logger := log.FromContext(ctx)
	h := s.human
	h.Status.Message = msg
	h.Status.ConsecutiveFailures++
	now := metav1.Now()
	h.Status.PhaseTransitionTime = &now

	if h.Status.ConsecutiveFailures > maxHumanRetries {
		h.Status.MaxRetriesReached = true
		logger.Info("human failed (max retries reached)",
			"name", h.Name, "username", s.username,
			"consecutiveFailures", h.Status.ConsecutiveFailures,
			"message", msg)
		// No error, no requeue: the Reconcile MaxRetriesReached guard keeps
		// the Human out of the queue until an operator re-arms it.
		return reconcile.Result{}, nil
	}

	delay := failBackoffFor(h.Status.ConsecutiveFailures)
	logger.Info("human reconcile failed, backing off",
		"name", h.Name, "username", s.username,
		"consecutiveFailures", h.Status.ConsecutiveFailures,
		"backoff", delay,
		"message", msg)
	return reconcile.Result{RequeueAfter: delay}, nil
}

func (r *HumanReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1beta1.Human{}).
		Complete(r)
}
