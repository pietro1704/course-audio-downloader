# Throughput and bounds

Use `course-deliver --concurrency N`, with N from 1 to 8. Browser resolution remains serialized to preserve lesson identity, while downloads, conversions and iCloud uploads overlap. The queue never submits the entire catalogue eagerly.

Retain at least 4 GiB of free disk. The pipeline reserves staging capacity per active lesson, rejects insufficient disk, and stops further submissions after failures. Sources survive failed conversion, upload or cancellation. More workers can saturate bandwidth, CPU or iCloud rather than improve throughput; compare completed lessons per minute and source-byte growth on the actual machine.

Embedded JSON profiles may opt into `hls_prefetch_fragments: 1..4` for VOD transport. Up to four 16 MiB fragments are fetched ahead in memory, with one current response in addition. FFmpeg still demuxes and decrypts ordinary AES-128 streams. Random routes on a loopback server replace remote addresses; signed URLs and keys are never written to a playlist on disk. Unsupported layouts retain the existing transport. Failure or cancellation closes the server and fetch pool and preserves partial output for normal recovery.

`aac_at` uses the macOS encoder; portable tests use `aac`. Both outputs must pass the same AAC-LC mono/44100 Hz/32 kbps validation and full decode. Native cloud upload acknowledgement remains required before scoped cleanup. Do not infer upload from the existence of a local cloud file.
