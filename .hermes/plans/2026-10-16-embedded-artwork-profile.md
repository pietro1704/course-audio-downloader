# Embedded artwork from a declarative profile

Status: verified locally; CI tracked on the PR

An embedded JSON transport currently resolves the audio stream but discards available video artwork. Preserve the configured thumbnail URL from the same identity-bound payload in the audio candidate. Thumbnail field paths belong to local profiles, with no concrete provider or course names in application code.

Acceptance: through the public resolver and real conversion pipeline, source artwork has priority; otherwise a configured HTTPS thumbnail is attempted. Missing, malformed, insecure or unavailable thumbnails allow valid audio without artwork. Wrong lesson identity must not fetch iframe metadata; cancellation must propagate. Parse the identity-bound payload once. Existing signed media URLs stay in memory and never reach error/progress output.

Verification: demonstrate the regression before the fix, run focused tests and the project's full unittest gate, inspect the diff, run the real profile dry run, commit/push this scoped branch and open a PR against its parent branch. Wait for CI. Keep private configuration and runtime data outside the repository.


## Review and evidence
Standards: diff reviewed against parent 844b274. Only a profile-controlled JSON path and optional HTTPS thumbnail are added to the existing embedded resolver. No provider/course names, concrete IDs or signed URLs added to application code or logs. The identity-bound iframe payload is parsed once. Audio transport, upload confirmation and cleanup are unchanged.

Spec: the public resolver and real conversion regression both failed before the fix because the artwork was absent. The final complete suite passed 155 tests in 64.601 seconds. Coverage includes source artwork priority, configured object/array paths, missing/malformed/insecure/credential-bearing URLs, invalid indices, stale lesson identity and cancellation. The real external profile passed two authenticated dry-run probes; its configured thumbnail produced a readable 1600×900 JPEG. No findings remain. Commit/push and the parent-based PR must pass CI before batch execution.
