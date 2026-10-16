# Bounded iCloud delivery

The shared `course-deliver` command accepts a catalogue, site profile, working root, iCloud destination and Foundation-capable Python. It supports 1–8 simultaneous lessons. Never run two workers against the same root; `Documentacao/delivery.lock` protects it.

Each successful conversion produces a fsynced prepared receipt in `delivery-journal.json`, recording canonical source identity, literal title, source and output hashes, exact paths, audio parameters and artwork provenance. Publishing waits for native uploaded state, matching size and modification time. Only then can the exact matching original be removed, the working output removed and the native cloud cache evicted. Cloud placeholders remain available through iCloud.

A prepared receipt resumes publication without another download. Complete receipts verify native state and output identity. Modified originals, stale receipts, symlinks and unrelated cloud outputs fail closed. Interrupted artwork replacement has its own recoverable transaction. Cancellation drains in-flight work and preserves unpublished originals.

`delivery-status.json` reports completed count, active lessons and stages, elapsed stage times, heartbeat and free disk. A stale heartbeat is not evidence that the process is alive; confirm its PID separately. Dry run validates receipts and optional browser probes without modifying media or receipts.
