# Embedded JSON transport

Set profile mode `embedded-json`, an authenticated HTTPS origin, active lesson selectors and the allowed player host pattern. The adapter binds the iframe to the expected lesson before reading embedded JSON. Configure `script_id`, `data_path`, `assets_field`, `url_key` and `protected_field` for the site's document structure. Defaults describe a common Next.js document, not a particular course.

Only unprotected HTTPS HLS candidates are accepted. Download copies audio packets with a bounded source size and verifies the full expected duration. Use the shared `course-deliver` CLI and `--dry-run --probe 1` before delivery. Signed media URLs remain in memory.

Optional `thumbnail_path` is a list of JSON object keys or array indices locating the video thumbnail URL in the same iframe payload. Only HTTPS image URLs with a host and no embedded username are accepted. Original attached artwork takes priority; missing, malformed or unavailable thumbnails leave valid audio without a cover.
