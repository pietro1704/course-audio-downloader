# Embedded JSON transport

Set profile mode `embedded-json`, an authenticated HTTPS origin, active lesson selectors and the allowed player host pattern. The adapter binds the iframe to the expected lesson before reading embedded JSON. Configure `script_id`, `data_path`, `assets_field`, `url_key` and `protected_field` for the site's document structure. Defaults describe a common Next.js document, not a particular course.

Only unprotected HTTPS HLS candidates are accepted. Download copies audio packets with a bounded source size and verifies the full expected duration. Use the shared `course-deliver` CLI and `--dry-run --probe 1` before delivery. Signed media URLs remain in memory.
