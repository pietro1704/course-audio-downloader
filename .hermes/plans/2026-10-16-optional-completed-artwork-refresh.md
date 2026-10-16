# Resume pending audio before optional completed-artwork maintenance

Status: implementing and verifying

A live resumed batch had four fully prepared audios but stopped before publishing them because startup automatically re-resolved one already completed, native-uploaded lesson whose prior optional thumbnail attempt returned no cover. The artwork lookup timed out, cancelling the whole resume. Valid completed no-cover receipts are supported outcomes; they should not make every restart depend on Safari or hydrate and rewrite an existing cloud output.

Make completed-artwork refresh explicit via `--refresh-completed-artwork`. By default, resume receipts and pending delivery directly. Keep first-pass source-cover priority and matching-video fallback unchanged. Explicit refresh retains all existing receipt/hash/upload/transaction checks and propagates failure or cancellation. Pending artwork transactions must continue to recover through normal delivery.

Acceptance: reproduce the default CLI startup failure before the change. Exercise successful default resume without refresh, explicit opt-in refresh, failure and cancellation, and rejection of a mismatched refresh receipt at the real Delivery boundary. Run the full project gate, a read-only native preflight and lock/build/diff checks; review, commit/push scoped PR against the extractor branch, and wait for CI before launching the new isolated engine. Publication of existing prepared receipts can continue through the already verified publication-only runner while CI executes.


## Review and evidence
The default-resume regression returned failure before the fix, and opt-in regressions rejected the unknown option. All four new CLI/Delivery boundary tests now pass, covering ordinary resume, explicit refresh, cancellation/failure, and wrong receipt identity before cloud access. The existing monitor-after-refresh regression now opts into the requested behavior. Full gate: 179 tests passed in 124.760 seconds. Real native dry run: 458 selected, 40 complete, 418 pending, zero transfers/probes. Lock consistency and wheel build passed.

Review against e3fe5fd: only explicit CLI policy changes; initial attached-artwork priority, video fallback, optional no-cover outcome, receipt identity, source cleanup and pending artwork transaction handling are unchanged. Explicit maintenance remains available, with all existing failure/cancellation checks. No provider/course identifiers or signed values introduced. No unresolved findings. CI must pass before a new engine is deployed. The pending native queue meanwhile runs on the previously verified engine with an offset beyond all40 completed receipts, avoiding startup maintenance without modifying active code.
