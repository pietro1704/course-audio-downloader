# Bounded audio pipeline: implementation and verification

## Scope

The optimized `ppr-audio` CLI handles a local PPR catalogue using an already authenticated Safari tab on macOS. The existing `course-audio-bot` entrypoint is preserved. This release does not claim complete IB or Hotmart catalogue adapters. Those providers must not be launched from the PPR adapter.

## Operation

Requirements: Python >=3.11, FFmpeg/ffprobe on PATH, macOS Safari with Apple Events JavaScript allowed, and an authenticated provider tab. Authentication never leaves the Mac.

Run a two-lesson smoke before a batch:

    ppr-audio --catalog /path/to/catalogue.json --output-root /path/to/output --limit 2 --concurrency 2 --min-free-gib 4

Use `--concurrency 1` for rollback to serial execution. Output names retain course title and catalogue course/lesson numbers. These numbers represent catalogue order, not verified publication chronology.

## Invariants

- Resolve Safari serially. Check authentication, canonical URL, expected title and stable media host/path, not rotating signature query strings. Never use the current front tab blindly.
- Prefer the explicit audio download control. This adapter refuses automatic video fallback: unavailable audio stops safely instead of downloading a multi-gigabyte video.
- At most 1–4 media workers; default 2. The executor submits at most that many tasks. A failure stops new submissions; already running work drains safely.
- Reserve disk before scheduling and extend the reservation using actual source duration/size. Source downloads are capped at 256 MiB; the CLI reserves 512 MiB per active task in addition to the 4 GiB free-space reserve.
- Move the downloaded source into immutable originals; retain changed sources under a hash-suffixed name. Never overwrite an original with an encoding.
- Encode AAC-LC/M4A mono, 44,100 Hz, target 32 kbps, unchanged volume/speed.
- Exactly one full output verification, including codec/profile/channel/sample-rate checks, bounded source-duration comparison and full decode, precedes atomic promotion.
- Preserve replaced finals via a same-filesystem hard link. If that cannot be made, stop without deleting the existing final.
- Resume without browser navigation or decode only for this validation schema with unchanged original/final hashes, title and preset. Legacy worker success flags alone are not migrated as trusted provider identity.
- Detect identical source hashes across different lesson identities. Preserve suspect work in quarantine and stop.
- Signed URLs are transient. Manifests retain only hashed identities, filenames, hashes, preset, timings and sanitized failure categories.

## Verification

    python -m unittest -v
    python -m unittest -v test_pipeline test_course_audio_bot
    python -m ppr_cli --help

Regressions cover ranking, wrong codec, complete verification exactly once before promotion, changed source/final hashes, cache without resolver, failure preservation, duplicate lessons/sources, concurrency bounds, serialized resolver, disk reserve, title transitions, rotating signed query strings and absent Safari authentication.

A Mac benchmark encoded two preserved real PPR originals (1,467.36 s and 1,904.90 s). The initial comparison measured 58.56 s serial and 34.08 s parallel (1.72x); corresponding final hashes were identical. Input download was a local copy, so this is not a claim about live network/Safari throughput. The benchmark is rerun on the release candidate separately.

## Rollback and known limitations

- Stop at a lesson boundary; use concurrency 1. Keep originals, prior finals, manifests and quarantine.
- Low disk or media failure returns nonzero; do not loop blindly over the complete catalogue.
- Catalogue completeness is distinct from successful downloads. Reconcile unique lesson identities before claiming completion.
- IB currently redirects to its login page; Hotmart catalogue integration remains separate work, not part of this PPR CLI.
- A fresh two-consecutive-lesson authenticated Safari test remains a release gate. Without that session, publish only a prerelease and do not resume the bulk.
- iCloud delivery and uploaded-file eviction are separate verified operations; the pipeline does not delete originals or claim cloud synchronization.
