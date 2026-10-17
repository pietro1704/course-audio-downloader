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

Profiles use `course-site-profile/v1`, HTTPS `origin` and mode `direct`, `embedded-json`, `template-hls` or `extractor`. Selectors, title suffixes, identity expressions, player patterns and URL templates belong to the local profile. Treat profiles as trusted local code: identity expressions execute read-only JavaScript in Safari. Examples and contracts are documented in [docs/template-profile.md](docs/template-profile.md).

Embedded profiles with `prefer_audio_rendition: true` prefer a dedicated audio
playlist. If all video variants explicitly reference one URI-less AUDIO rendition
and declare the same audio codec, resolution instead selects the variant with
the least video bandwidth. Ambiguous groups, distinct audio codecs, or missing
declarations retain the original master selection and source-quality policy.

If a provider publishes equivalent route prefixes, a profile may declare
`"url_path_prefix_aliases": {"/en-GB/": "/en-gb/"}`. Browser audio resolution and
document discovery normalize only these explicit prefixes when comparing the
current, requested and canonical page URLs. Embedded JSON resolution applies
the same aliases to the current page and active lesson path. Origins and the rest of each path,
including case-sensitive lesson IDs, must still match. Default comparisons stay
strict. Catalogue URLs, persisted identities and raw document page guards stay
unchanged; this option does not rewrite stored receipts or bypass page checks.

The bounded pipeline supports 1–8 lessons concurrently, with browser resolution serialized. Audio is AAC-LC mono, 44100 Hz, target 32 kbps. Original attached artwork takes priority; absent artwork falls back to the matching video thumbnail, then allows audio without a cover. Validate actual throughput before increasing concurrency further.

Repeated source bytes across distinct lessons are rejected by default. When the
site intentionally reuses a recording, each affected catalogue lesson may list
the other lesson URLs in `shared_source_with`. Permissions must be reciprocal
and reference lessons in the same catalogue. This permits identical source bytes
only among the declared peers; every lesson still resolves and validates its own
source and output, preserving its title, path, and identity. Manifests store only
the peer identity hashes. Multiple owners remain registered across retries so
failure or cancellation cannot remove another lesson's source ownership.

Delivery uses `Documentacao/delivery.lock`, `delivery-journal.json`, `delivery-status.json` and `pipeline-manifest.jsonl`. A verified native iCloud upload is required before deleting the matching original and evicting the local cloud cache. Prepared receipts resume without another download. A completed no-cover receipt does not repeat its prior optional artwork lookup on restart; use `--refresh-completed-artwork` explicitly to retry missing covers. First-pass embedded artwork and matching-video fallback always run during conversion. Cancellation drains submitted tasks and preserves sources. See [delivery details](docs/sequential-icloud.md).

For older receipts without an artwork policy result, `--reconcile-original-artwork`
recovers the original from the identity-checked lesson and requires its historical
SHA-256 before restoring attached artwork. Missing original artwork falls back to
the matching video thumbnail, then records `origin: none`. This mode preserves
the encoded audio stream, uses bounded concurrency with serialized navigation,
and resumes the existing artwork publication transaction. Changed source bytes
stop safely, preserving the failed download and existing cloud audio. Local
sources remain until native upload confirmation, then the cloud cache is evicted.
Per-lesson source blockers are recorded in `artwork-attempts.json` while other
lessons continue; cloud, disk and browser failures still stop the lane for bounded
supervisor recovery. Any unresolved artwork blocker makes the run fail.

Run the same regression gate as CI (Node 22+ is needed for generated browser-JavaScript contract tests):

```sh
python -m unittest -v
```

The generic `course-audio-bot` command also supports the older TOML/yt-dlp workflow; see `config.example.toml` and `course-audio-bot --help`. It does not use the native iCloud delivery receipts.

External embeds can use the optional `extractor` profile mode after `uv sync --extra extractor`. Its `extractor.url_pattern` is a full HTTPS URL regex with a named `id` capture; the adapter validates extracted metadata against that identity (and optional lesson `external_media_id`) and downloads one bounded HTTPS audio-only format. Unsupported/live/protected media fail without publishing. Browser cookies are not exported. Use `links_selector` to scope authored media links; `browser_target: {"window_id": 42, "tab_index": 3}` optionally binds resolution to an existing dedicated Safari tab and fails if it changes origin. This allows another worker to use a separate tab. Keep those ephemeral window/tab values in private profiles.

`course-supervise --spec /path/to/private-supervisor.json --dry-run` validates a fixed queue and isolated engine without writes or launches. Remove `--dry-run` to adopt the configured live worker and retry only failed runs after receipt/native-cloud preflight. It reaps owned children, detects changed process identity, refuses changed queue scope, serializes supervisors per root and bounds retries without progress. An interrupted worker stays stopped; cancelling monitoring leaves active downloads running. The private spec supplies `root`, `catalog`, `profile`, `destination`, `icloud_python`, worker `python`, immutable `engine` (with `engine.json` file hashes), `initial_worker_pid`, `start`, `concurrency` and `expected_partition_count`; optional `upload_timeout` overrides the 900-second CLI default and is bounded to 1–21600 seconds. The matching `course-deliver --upload-timeout` option uses the same bound. It never closes or selects Safari windows/tabs. Existing configured browser-target identity checks apply to every worker.

Embedded JSON profiles can opt into `navigation_mode: "matching-link"` to reuse the site's initialized application through a matching lesson link. Declare any ignorable tracking parameters in `navigation_ignored_query_keys` (default: none). Other query values, origins, new-window targets, download links and disabled controls never match. First resolution, missing links, revisits and changed predecessor/player state use the original URL-assignment reload, including credential refresh. A one-use context is granted only after successful media resolution, so failed navigation or metadata cannot authorize a subsequent click. Existing stable page/player/media identity checks remain required. Measure with the real site before enabling this option; UI timing does not guarantee whole-course throughput.

`course-supervise-jobs --spec /path/to/private-jobs.json` supervises sequential
trusted commands, including document backfills and artwork reconciliation.
The spec supplies `state`, a shared browser `lock`, and `jobs` with unique `name`,
argument lists `preflight`, `command`, `verify`, and a private `log`. Verification
must audit the selected catalogue and native cloud receipts; exit zero alone
does not complete a job. Optional `progress` entries supply a JSON journal `path`,
dotted `field`, and accepted `values` for durable completion counts.
`max_no_progress` (default 3) and `max_attempts` (default 100) survive restarts.
Failed preflights and dead workers retry within those bounds; blocked jobs are
recorded while later jobs continue. Exit 130 and signals SIGINT/SIGTERM leave a
worker cancelled. Cancelling monitoring keeps its worker running, and another
monitor refuses to overlap it. A changed spec fails closed; optional `bindings`
of `path` and `sha256` also pin engine and input files. Independent lanes must
bind separate existing tabs. Status records exit codes, progress, retry limits
and available disk; errors remain in the private job log.

Failure manifests and terminal status retain known transfer reason codes such as
`embedded-lesson-identity-timeout`, `login-required` and `disk-reserve`. Document
pages also retain the failed inventory reason and timestamp. Unknown exception
messages are replaced by their type so signed URLs and other private values do
not enter public diagnostics.

### Documentos das aulas

`course-deliver` também inventaria e entrega os documentos originais de todas as
aulas selecionadas, inclusive quando o áudio já está concluído. Os documentos
ficam em `curso/módulo/aula/nome original`, sem conversão. A limpeza local exige
confirmação nativa do iCloud e comparação dos bytes. O diário de documentos é
separado do diário de áudio e permite retomar uma publicação interrompida.

Use `--documents-only` para preencher os anexos sem baixar ou converter áudio.
`--documents-only --dry-run` valida as transações existentes sem criar arquivos;
`--probe N` também visita N aulas para conferir o inventário, sem baixar anexos.

O perfil local deve declarar `browser_target` para a aba dedicada. Por padrão, a
descoberta procura links com `download` ou extensões de documentos. Sites com
painéis dinâmicos podem declarar `documents.links_selector`, `ready_expression`,
`items_expression` e `panel_script`. Essas expressões JavaScript são configuração
local confiável, executada somente após verificar URL, título e identidades da
aula. `items_expression` retorna uma lista de `{id, name, url, size?, mime?}`;
`id` precisa ser estável mesmo quando a URL assinada muda. URLs de download ficam
somente em memória. Não inclua credenciais no perfil ou no repositório. Recursos
de áudio e vídeo são excluídos do inventário de documentos. Sessão expirada e
três falhas consecutivas de inventário encerram a fila preservando os originais.
