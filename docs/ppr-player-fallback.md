# PPR fallback when the lesson has no MP3

## Scope and source order

Keep the approved AAC-LC M4A, mono, target 32 kbps, 44100 Hz with no speed, volume or noise processing changes. Preserve originals. The actual lesson's explicit MP3 download control remains first choice, including extensionless CDN addresses. Only when direct audio is absent may the verified Bunny iframe become a fallback.

## Prerequisites and procedure

Use the existing authenticated Safari PPR tab with JavaScript from Apple Events enabled by the user. Keep other providers stopped. Match the requested lesson URL and title, authentication, document completion and stable frame host/path before fetching the player. The provider frame must be HTTPS player.mediadelivery.net/embed/<library>/<video>; HTML library-id/video-id must match it. Require drm-version=0; unsupported/DRM players fail closed.

Read content-src from bunny-stream-video via HTMLParser. Keep player/HLS addresses in memory only, never raw HTML or signed URLs in logs. Parse the HLS master and prefer an explicit audio rendition, then the highest-bandwidth audio-only variant. If only muxed variants exist, select the highest advertised bandwidth: actual PPR samples showed the 240p source audio at about 100 kbps and 1080p at about 249 kbps, both AAC 48 kHz stereo, so lower video resolution does not preserve the same source audio. Preserve source audio by stream copy before the approved final AAC32 encode. Muxed fallback transfers video over the network but does not save the full video to disk.

For plain MPEG-TS VOD, fetch four fragments concurrently in ordered batches of four, cap each at 16 MiB, retry incomplete/network failures at most three times and stream the ordered bytes into FFmpeg, retaining only the copied audio output. Check HTTPS, content type, fragment length, finite segment duration, final source size and the complete EXTINF duration sum against the saved original. Do not eagerly submit a whole movie to executor.map. Map/byte-range or encrypted container modes use the existing guarded FFmpeg downloader rather than the plain-TS path. Live playlists are rejected. Pipeline disk reservations remain unchanged.

Both HLS paths establish finite VOD duration before transfer, cap the preserved source size, and compare downloaded duration against the expected duration (EXTINF sum for plain TS, remote probe for the shared FFmpeg path). The normal pipeline then encodes and fully validates codec/profile/channels/rate/bitrate/source duration and decoding before atomic promotion. Retain failures in quarantine and existing valid outputs unchanged.

## Verification and restart

Run the complete unittest suite, build and test wheel plus source distribution, and exercise the exact previously failing lesson plus following distinct lessons. Compare current hashes, media identities and preserved source metadata. A download still growing or a playable prefix does not pass the gate. Keep provider catalogue counts separate from verified outputs. Activate bounded workers only after the unchanged-preset local concurrency benchmark and fresh identity checks pass; Safari navigation remains serialized within the one active provider.

For a limited test use ppr-audio --catalog <catalog.json> --output-root <root> --start <verified-index> --limit 3 --concurrency 2 --min-free-gib 4. For a library restart reconcile successful manifest identities and current files rather than relying solely on a numeric offset.

## Rollback and known failure modes

Use --concurrency 1 without changing encoding. The previous release retains MP3-only support but cannot pass missing-MP3 lessons. Do not skip missing-source lessons or declare them completed. A new provider iframe schema or a non-Bunny/DRM player requires an explicit tested adapter; it is not permission to bypass access controls. Disk reserve remains at least 4 GiB and originals are never deleted automatically.
