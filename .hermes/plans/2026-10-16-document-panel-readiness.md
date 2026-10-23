# Honor an explicit document panel readiness contract

Status: live bottleneck reproduced; regression red before fix.

Source evidence: during four independent backfill partitions, an authenticated lesson had the verified title and a mounted downloads panel while document.readyState was interactive. DocumentBrowser nevertheless required complete before checking its explicit ready_expression. Unrelated video/frame loading can hold that browser state and cause avoidable timeouts.

Change: allow interactive source inspection only when the trusted profile declares a document readiness expression. Continue binding the exact URL/title/auth/canonical/identities, then require that explicit resource readiness expression before returning any inventory, including an empty one. Give panel readiness its own bounded wait after source verification. Generic discovery without an explicit contract still requires complete.

Acceptance: the new regression must fail on the old implementation and pass with the fix; interactive generic pages and explicit not-ready panels must never report empty success. Existing identity, cancellation and generated JavaScript guards remain covered. Run the full project gate and dry run; scoped commit/PR with CI green before creating a new isolated runtime. Existing workers retain their original snapshots until a deliberate, receipt-preserving handoff.

Additional live evidence: Safari can return an empty inspection during navigation. The previous loop treated that transient parse error as a permanent lesson failure. A second regression was red before adding bounded inspection retries; cancellation and login still propagate, and permanently unavailable pages never execute the panel.

The readiness tests use a deterministic clock to avoid asserting millisecond scheduling on the busy download host. Generated JavaScript still executes in Node with a bounded process deadline; its cold-start allowance is 15 seconds. A previous full run hit the old 5-second Node startup limit; another exposed a 1 ms wall-clock test race. Both are recorded rather than claimed green. The final complete gate is recorded after the deterministic tests pass.

Final local gate: all 225 tests passed in 133.839 seconds using the application's Python 3.11 runtime. Two pre-existing fixtures also reproduced scheduler sensitivity under load: embedded artwork used a real 1-second clock for mocked observations, and navigation allowed only 5 seconds for Node startup. The artwork fixture now advances a deterministic clock; navigation uses the same 15-second bounded Node allowance as document guards. Assertions and product timeouts are unchanged.

Review against a7af101: source checks remain mandatory, generic interactive pages remain rejected, explicit unready panels cannot report empty success, and retries are bounded with cancellation outside the catch. No provider identities, source filenames, credentials or signed URLs were added. Live read-only discovery reached three authenticated lessons of the second catalogue; its independent document worker is now running on the prior CI-approved snapshot.
