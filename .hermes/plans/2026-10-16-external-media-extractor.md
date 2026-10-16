# Extract audio from externally embedded lesson media

Status: implementation and regression verification

Some authorized lesson pages embed externally hosted video rather than the site's native player. Add an optional extractor adapter with a declarative URL pattern that captures a media identity. Resolve the current lesson through the existing stable browser URL/title/active identity checks, then validate extractor metadata against that captured identity. Keep source URLs, domains and lesson names in private profile/catalogue data.

Use yt-dlp's documented Python interface as an optional dependency, selecting one HTTPS audio-only format. Reject playlists, live/DRM content, unknown durations, changed media IDs and unsafe transport. Keep extracted signed metadata in memory only, silence third-party logs, cap declared and actual downloaded bytes, verify duration before accepting the source. Preserve source artwork priority and the matching video's thumbnail fallback through existing delivery. No browser cookie export or authentication changes. An optional validated window/tab target binds the external worker to its own existing Safari tab without navigating another worker's lesson; a missing/replaced/wrong-origin target must fail without fallback.

Acceptance: meaningful adapter tests for resolution/download success, wrong lesson/media identity, missing dependency, bounds, failure, cancellation and optional artwork. Demonstrate red before implementation. Run the full project gate, real read-only probe/dry run, review diff, commit/push a scoped PR against the parent branch and wait for CI before deploying a separate immutable engine. Do not mutate the active download worker.


## Review and evidence

The extractor and targeted-tab regressions failed before their implementations; an additional unknown-size byte-budget test exposed an error-classification bug and passed after fixing it. The public adapter currently passes 17 focused cases, including successful source download, page/media identity, ambiguity, size guards, truncation/nonfinite duration, cancellation, collision, missing dependency and artwork fallback. The real private 17-item external catalogue passed read-only preflight plus one authenticated browser/metadata probe; its working root was not created. Full regression gate passed 175 tests in 120.389 seconds; all 17 focused cases also passed after the final duration-bound adjustment. Wheel build and lock consistency passed. CI results are recorded on the PR.

Review against the parent commit: no concrete course/provider domain or identifier in application code/tests/docs. Source titles and hierarchy are external data; native receipt/deletion machinery is unchanged. Signed extractor metadata remains in memory and third-party errors/logs are sanitized. Dedicated tab selection validates positive integer window/tab values and origin, with no fallback. Extracted media must be one finite, unprotected HTTPS audio-only format matching the observed URL and optional catalogue identity. Actual byte/duration validation precedes promotion. No unresolved review findings; deploy only after full checks and CI pass.

API reference: https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp (documented YoutubeDL options, extract_info and process_info; inspected 2026-10-16).
