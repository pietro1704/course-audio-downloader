# Course Audio Downloader

A small local CLI for downloading **personal offline audio** from course pages you can already access in your browser.

It uses exported browser cookies, crawls only configured/same-site lesson links, extracts media URLs exposed by the authenticated page, and writes a manifest for auditing. It does **not** bypass DRM, encryption, paywalls, or account controls.

## Defaults

- Default output format: **M4A/AAC** for iPhone/Apple apps.
- Speech-friendly compression: AAC-LC M4A, mono, 44.1 kHz, 32 kbps, without normalization. Each lesson is staged, ffprobe-checked and fully decoded before atomic promotion; originals are retained under `originals/`.
- MP3 is opt-in only: pass `--mp3` or `--audio-format mp3`.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for local execution
- `ffmpeg` and `ffprobe`
- `yt-dlp`

## Install

```bash
uv sync
```

## Configure

Copy the example config and edit it locally:

```bash
cp config.example.toml config.toml
```

Example:

```toml
output_root = "~/Course Audio"

[[course]]
provider = "example-provider"
title = "Example Course"
cookies_file = "~/Secrets/course-cookies/example.cookies.txt"
crawl = false
urls = [
  "https://example.com/course/lesson-1",
]
```

Keep cookies and real course URLs outside git. The `.gitignore` excludes `config.toml`, `config.*.toml`, cookie files, logs, manifests, and downloaded media.

## Export cookies safely

Export Netscape-format `cookies.txt` from the browser profile where you are already logged in. Store it outside the repository, for example:

```text
~/Secrets/course-cookies/example.cookies.txt
```

Do not commit cookies, tokens, account IDs, private lesson lists, or downloaded media.

## Run

Default M4A/AAC:

```bash
uv run course-audio-bot --config config.toml
```

Skip the initial `yt-dlp` page attempt and scrape the authenticated page first:

```bash
uv run course-audio-bot --config config.toml --no-ytdlp-first
```

Explicit MP3 override:

```bash
uv run course-audio-bot --config config.toml --mp3
```

Audit one page without downloading:

```bash
uv run course-audio-bot audit-url https://example.com/course/lesson-1 \
  --cookies-file ~/Secrets/course-cookies/example.cookies.txt
```

## How it works

For each configured URL the CLI:

1. optionally tries `yt-dlp` with cookies;
2. fetches the authenticated HTML page;
3. extracts direct audio/video/stream URLs visible in the page;
4. handles common signed audio URLs and HLS streams;
5. derives Panda Video HLS URLs from public iframe parameters when present;
6. downloads/transcodes to compact M4A by default;
7. appends a `manifest.jsonl` record with status and details.

## Safety boundaries

This tool is for lawful personal offline access to material you can already open with your own account. It intentionally does not defeat DRM or technical access controls. If a page only shows login/subscription/lock UI, refresh your browser session and export cookies again; do not attempt to bypass the access check.

## Tests

```bash
uv run python -m unittest -v
```

The test suite covers URL extraction, crawler filtering, signed-media detection, output format defaults/overrides, local downloads, and repository hygiene checks for private files.
