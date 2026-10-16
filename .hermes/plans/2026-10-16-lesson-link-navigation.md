# Opt-in navigation through matching lesson links

Status: benchmark, implementation and verification

Reloading the entire authenticated site discards its initialized application state between lessons. A paired warm-destination browser comparison measured4.535s for reload and1.587s through the site's own link (2.86x for this UI readiness sample, not whole-course throughput). The link preserved performance.timeOrigin; the original DOM URL, title and player checks still apply.

Add optional profile navigation_mode=matching-link with explicitly declared navigation_ignored_query_keys. Click only a same-origin, exact normalized URL anchor that is neither a download, disabled nor another-window target. Otherwise use the existing AppleScript URL assignment. Navigating to the current lesson must also reload to refresh expired player credentials. Preserve default behavior and all stable title/URL/active/player/media identity checks.

Acceptance: execute generated JavaScript under Node with successful link selection and negative URL/query/target/download/disabled/malformed cases; test fallback through original browser assignment, cancellation, invalid profile data and stale player rejection. Demonstrate red before implementation. Repeat real paired comparison against the authoritative media ID, run full gate plus real dry run/probe, review, commit/push scoped PR against parent, wait for CI. Existing worker is intentionally draining current source/conversion tasks; interrupted state must remain stopped until the verified handoff. Never close Safari windows or user tabs.


## Review and evidence
Final scope is opt-in for the guarded embedded JSON adapter. A one-use predecessor context is granted only after successful media resolution, consumed before navigation, and checked against the current document path and first iframe origin/path. First resolution, uncertain/changed context, failed navigation, current-lesson revisits or absent safe anchors reload through the original URL assignment. Existing stable title/URL/active and stale-player checks remain intact; other adapter modes reject this option.

The generated JavaScript tests failed before implementation and now run under Node, including wrong origin/query, other-window/download/disabled controls, malformed URLs, predecessor/player changes and self-navigation. Additional Python cases cover first reload, fallback, cancellation and consumed context; the existing stale-player regression runs with the option enabled. Full gate:199 tests passed in62.415s. Ten navigation tests and21 embedded tests passed together. A repeated real paired benchmark required the authoritative media code for every ready observation:4.123s reload vs1.689s link (2.44x UI sample), with unchanged document timeOrigin. Real CLI dry run probed two future native lessons and observed first reload followed by link-clicked, with no media transfers or writes. Lock check, wheel and diff checks passed.

Review: course identities and tracking keys remain private profile/catalogue data; default behavior, audio/cover processing and native upload/cleanup are unchanged. No unresolved findings. Wait for CI before using a new isolated snapshot and resuming the intentionally interrupted worker. The source files and prepared receipts were retained during drain; no live worker is to be restarted merely for an observation timeout.
