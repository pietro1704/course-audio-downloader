# Default lesson document delivery

Status: implementation and local regression checks complete; external CI/deployment and full catalogue backfill pending.

## Requested behavior

Keep the active full course download and its resource queue running. Backfill every lesson document from both previously completed course catalogues in parallel. Document discovery and delivery must become the application default, including on audio-complete lessons, with a document-only backfill mode. Preserve literal names and the course/module/lesson hierarchy. All provider contracts, selectors, origins and course identities belong to private input data.

## Implementation requirements

- A document module owns source-bound inventory, bounded download, filename/size/hash checks, native iCloud publication, resumable receipts and cleanup after confirmed upload. Reuse existing tested cloud stages.
- Generic page adapters must support at least authenticated direct document links and declarative resource inventories. No provider/course names or IDs in application code, tests, commands or internal filenames.
- Bind resources to the verified lesson URL/title and configured page identity before requesting documents. Preserve original formats and exact source filenames. Reject login HTML and mismatched resources; retain failures and cancellation for resumption.
- Default delivery must inspect documents even when audio is already complete. Document-only backfill must not resolve/download/convert audio or modify audio receipts.
- Dry run must report audio and document scope without transfers, working-file writes, publication or cleanup; explicitly requested probes may inspect browser inventory.
- Respect active run locks and immutable engines. Use separate document roots and dedicated existing browser targets for concurrent catalogues. Do not close Safari windows or alter active download tabs.
- Resolve filesystem compatibility using evidence from actual native-uploaded outputs, preserving literal supported names. Never silently replace directory separators.

## Acceptance evidence

1. Meaningful runtime regression tests: default CLI behavior, audio-complete backfill, two unrelated profiles/catalogues, direct links and declared inventories, exact hierarchy, source/resource identity rejection, failed download/upload, cancellation, cleanup and restart.
2. Demonstrate new regression red before implementation where feasible.
3. Full project test gate, generated-JavaScript tests, hygiene and final diff inspection.
4. Live read-only probes/dry runs against both backfill catalogues and existing active-course document contracts.
5. Scoped commit, branch push and PR; required CI green before deploying a new isolated engine.
6. Start backfill queues, verify actual processes and report document counts/speed separately from audio. Keep unresolved items and the original full course scope visible; do not claim all documents without lesson-wide inventory and native receipts.

## Outstanding discovery

- Recover private legacy catalogue paths and exact output parent mapping.
- Inspect current authenticated document controls on the two backfill sites.
- Confirm literal colon support against an existing native iCloud receipt before changing filename validation.
- Preserve current pending filenames, artwork retries and inaccessible external documents in the full task inventory.

## Implementation / review evidence

- Shared FilePublisher extracted mechanically from the existing native publication and confirmation stages; 33 existing transfer/bridge tests passed.
- New initial CLI tests were red before implementation (missing default documents, unsupported documents-only, rejected colon).
- Source-bound DocumentBrowser supports direct anchors and trusted declarative inventories/panels. Generated JavaScript tests cover two unrelated profiles, mounted panels via mousedown, and changed URL/title/auth/identity guards.
- Independent DocumentDelivery uses bounded streaming, PDF/Office verification, exact original names, exclusive publication, native confirmation, hashed resource identity and resumable transaction journals. Failed or cancelled transfers preserve local bytes.
- Documents run by default after successful audio delivery, even when every audio receipt is complete. Explicit documents-only bypasses audio construction. Existing CLI policy tests now mock this separate stage explicitly.
- 20 focused document/CLI/browser tests passed. Final full suite: 219 tests passed in 71.341 seconds; wheel built and all three new runtime modules verified in the archive.
- Full 1,009-lesson and 67-lesson document dry runs passed without creating their work roots. Authenticated sample found original PDFs as well as audio-only lessons. The other site's old cached page returned to login upon actual navigation; authentication requested while independent work continues.
- Native query of an existing uploaded colon-containing output confirmed exact size and metadata; literal colon allowed, directory separators still rejected.

Review axes (working diff against a029c99): standards preserve unrelated work, existing active engines, no course identities or signed URLs in code/journals; spec delivers a complete generic document slice. Operational acceptance is intentionally pending: second authenticated site probe, full backfills and native final verification. No completion claim for the overarching download goal.
