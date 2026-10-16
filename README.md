# Course audio downloader

Personal downloader for courses you can access in an authenticated browser. Site contracts and course identities are input data; the application contains no course-specific adapters.

Requires Python 3.11+, ffmpeg/ffprobe and an authenticated Safari session with JavaScript from Apple Events enabled. Native iCloud delivery requires macOS and a Python environment with PyObjC Foundation.

Install with `uv sync`, then use `uv run course-deliver`. Keep private catalogues and site profiles outside the repository.

```sh
course-deliver --catalog /path/to/course-catalog.json \
  --profile /path/to/site-profile.json --root /path/to/work \
  --destination '/path/to/iCloud Drive/Courses' \
  --icloud-python /path/to/foundation/python --concurrency 8 --dry-run --probe 4
```

Remove `--dry-run --probe 4` to deliver the selected catalogue. Dry run checks catalogue identity, literal destination paths and existing receipts without creating working directories, downloading media, converting, publishing or deleting files. Optional probes navigate the authenticated browser and resolve pending media without transferring it.

Catalogues use `course-catalog/v1`, complete counts and lesson `title`, `url`, `site_path`; optional `page_title` and `page_identity` bind site headings and IDs. The final component of `site_path` is the exact lesson title; the output adds `.m4a`. Module catalogues may instead provide `title`, `modules[].title` and `modules[].lessons`. Legacy unversioned catalogues are accepted; unknown schemas are rejected.

Profiles use `course-site-profile/v1`, HTTPS `origin` and mode `direct`, `embedded-json` or `template-hls`. Selectors, title suffixes, identity expressions, player patterns and URL templates belong to the local profile. Treat profiles as trusted local code: identity expressions execute read-only JavaScript in Safari. Examples and contracts are documented in [docs/template-profile.md](docs/template-profile.md).

The bounded pipeline supports 1–8 lessons concurrently, with browser resolution serialized. Audio is AAC-LC mono, 44100 Hz, target 32 kbps. Original attached artwork takes priority; absent artwork falls back to the matching video thumbnail, then allows audio without a cover. Validate actual throughput before increasing concurrency further.

Delivery uses `Documentacao/delivery.lock`, `delivery-journal.json`, `delivery-status.json` and `pipeline-manifest.jsonl`. A verified native iCloud upload is required before deleting the matching original and evicting the local cloud cache. Prepared receipts resume without another download. Cancellation drains submitted tasks and preserves sources. See [delivery details](docs/sequential-icloud.md).

Run the same regression gate as CI:

```sh
python -m unittest -v
```

The generic `course-audio-bot` command also supports the older TOML/yt-dlp workflow; see `config.example.toml` and `course-audio-bot --help`. It does not use the native iCloud delivery receipts.
