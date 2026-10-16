# Select the declared audio rendition before probing and copying

Status: verified locally; CI tracked on the PR

The embedded transport currently sends the full HLS master to probe/copy, even when it declares a separate default audio rendition. An authorized 15-second comparison returned the same 647 AAC packet hashes in 3.749 seconds from the master and 1.231 seconds from the audio rendition. This is a sample result, not a full-course speed guarantee.

Add an opt-in profile setting to select a declared audio-only rendition. Preserve existing behavior without the setting, and retain the original master when no audio-only rendition exists. Bind selection to the already verified iframe payload; preserve artwork and referer. Keep FFmpeg's guarded copy, size/duration checks and authenticated stream handling unchanged. Do not enable the parallel fragment transport on encrypted playlists.

Acceptance: regress the actual public resolver; cover dedicated audio, absent rendition fallback, malformed/insecure manifests, cancellation and wrong lesson identity. Run full tests, real profile dry run and inspect diff. Commit, push and open a scoped PR on the parent branch; wait for CI. Deploy an isolated engine snapshot so the running worker's installed bridge is not replaced. Drain existing work before switching its remaining queue; preserve all prepared receipts and imported sources.


## Review and evidence
Standards reviewed against 6cd8345: one opt-in profile flag, protocol-only selector change, no concrete provider/course identifiers or media URLs. Existing copy/decryption behavior and cleanup guards unchanged. The selector retains the original master if no declared audio-only option exists; it does not choose a different muxed video quality. Default behavior remains unchanged.

Spec: four public-resolver regression assertions failed before the fix. Full gate passed 158 tests in 77.602 seconds, including valid audio selection, artwork/referer preservation, absent-rendition fallback, opt-out, malformed/insecure playlists, cancellation and stale lesson identity with the flag enabled. Real dry run passed across the 458-item tail with two authenticated probes. The prior 15-second comparison preserved all 647 packet hashes and measured 3.05× for that sample only. No unresolved code findings. CI must pass before launching the isolated snapshot. Existing worker is draining its 26-item prefix; its prepared files and two out-of-prefix imported sources must be preserved.
