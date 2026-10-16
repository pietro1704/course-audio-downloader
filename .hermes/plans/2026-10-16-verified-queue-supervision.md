# Supervise verified delivery queues without mistaking zombies for live workers

Status: implementation and verification

The operational resume monitor used kill -0 on its own Popen child and never reaped it. A failed child remained a zombie, so the queue was incorrectly considered active forever. Add a course-neutral supervisor with explicit process ownership: poll/reap owned children; inspect kernel state and start identity for an adopted worker. Never restart a live worker, reuse a changed PID, override an interrupted run, or terminate a download when monitoring stops.

Before a retry, require matching terminal worker identity, a valid isolated engine manifest, unchanged queue scope, a read-only receipt/native-cloud preflight and an available delivery lock. Bound retries without progress and total monitoring time. Serialize supervisors per working root. Preserve failed sources and stop on identity/preflight failures. Full partition count is explicit and cannot shrink to the completed subset. Dry run performs no writes or launches.

Acceptance: reproduce zombie liveness failure before fixing it; cover actual child success/failure/reaping, retry to completion, cancellation leaving the child alive, queue/PID/engine identity failures, interrupted runs and read-only dry run. Run full project checks and real native/external supervisor dry runs, inspect diff, commit/push scoped PR against the parent branch and wait for CI. Attach the tested supervisor to current live workers only after CI, without replacing or restarting those workers.


## Review and evidence
The real zombie regression fails with the old kill -0 implementation and passes with ownership-aware polling. Ten focused cases cover actual child reaping, a real failed child followed by success, adopted PID changes, bounded no-progress retries, cancellation without stopping a live child, interrupted workers, incomplete terminal queues, engine/queue/preflight identity failures, duplicate-monitor exclusion and helper reaping. Full project gate: 189 tests passed in 68.142 seconds. Real dry runs passed for native458 (71 complete) and external16 (11 complete). Lock consistency, wheel build and diff checks passed.

Review: no course/provider identities or signed URLs in code, tests or documentation. Supervisor uses the existing read-only/native preflight and verified immutable engine, never deletes media or changes Safari, and adopts live workers without restarting them. Separate supervisor and delivery locks prevent concurrent ownership; failed identity/preflight inspection stops supervision with the worker intact. Child poll/wait semantics now distinguish exit from zombie liveness. Completed counts cannot shrink the fixed selected partition. No unresolved findings. CI is required before attaching this module to live queues.
