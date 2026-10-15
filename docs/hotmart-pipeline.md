# Hotmart / Mira authenticated audio candidate

## Goal and scope

Use the bounded course-audio pipeline with the existing authenticated Safari Mira lesson tab. Preserve provider audio by stream copy, encode AAC-LC mono 32 kbps at 44100 Hz, validate completely and compare duration before promotion. No volume/noise/speed changes. This adapter targets the currently observed Mira product on hotmart.com, not arbitrary Hotmart products or hostnames.

## Prerequisites

macOS, Safari JavaScript from Apple Events enabled by the user, authenticated Mira product 539478 tab, ffmpeg/ffprobe available, at least 4 GiB free plus 512 MiB reserved per active task. Authentication stays on the Mac. No cookies or signed URLs belong in the catalogue, source tree or manifests. DRM-marked players are rejected, not bypassed.

## Procedure

1. Extract the authenticated sidebar module by module into a sanitized catalogue: modules contain order/title/lessons, each lesson contains title/url/number. Check unique canonical content IDs and account for collapsed or virtualized sections. The observed sidebar has 42 modules and 586 unique contents; this is not an audio lesson count or independently proven provider total. Certificates, live sessions and support material need reconciliation.
2. Run a two-consecutive-lesson smoke before a bulk run:

       hotmart-audio --catalog /path/to/catalog.json --output-root /path/to/Mira-AAC32 --smoke-module

3. For a limited selection, use --start and --limit. --concurrency defaults to 1 and supports the pipeline bounds; use 2 only after a representative throughput/correctness test. Safari navigation remains serialized regardless of concurrency. --min-free-gib cannot go below 4.
4. The adapter matches current URL, lesson heading and active sidebar lesson title/path. It waits for a stable player iframe, and refuses a previous lesson's player on a new identity. Only the expected cf-embed player is fetched. HLS URLs come from props.pageProps.applicationData.mediaAssets; they are retained only in memory.
5. FFprobe obtains finite VOD duration. FFmpeg preserves only audio using -map 0:a:0 -c:a copy, capped at 512 MiB; downloaded duration must match before encoding. Original audio is kept under originals, failed work under quarantine. The approved pipeline performs codec/profile/channel/rate/bitrate/duration/full-decode checks before final promotion.

## Verification

Use python -m unittest -v for full regression coverage, including strict asset parsing, stale iframe rejection, DRM rejection, source budget, truncation, stream-copy arguments, encoder failure and pipeline promotion/resume invariants. Install the built wheel and test hotmart-audio --help; also run tests from the unpacked source distribution. Two distinct provider identities and source hashes plus preserved originals and valid output are the online smoke gate. Cached skips are not a fresh online identity test.

## Rollback and known limitations

Use --concurrency 1. Keep originals, finals, manifests and quarantine; do not erase sources or task history. Previous PPR-only rc1 remains separately published. The candidate does not provide IB authentication/catalogue support. Anonymous PPR or an IB login page blocks those providers, not the authenticated Mira session.

The master HLS may multiplex video with audio; -map controls the preserved output, not necessarily all network traffic. Do not claim audio-only transfer or network acceleration from a small output file. Download throughput remains unoptimized and must be measured. No bulk-complete claim is valid until all expected audio identities and non-audio contents are reconciled.
