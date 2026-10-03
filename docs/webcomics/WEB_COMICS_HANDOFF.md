# Webcomics implementation handoff — 2026-09-29

The repository now has a working provider-neutral Webcomics ingestion foundation integrated into the Python/Tkinter app. This is a first implementation milestone, not universal platform coverage. Start with the [README](../../README.md), [architecture](WEB_COMICS_ARCHITECTURE.md), [provider matrix](WEB_COMICS_PROVIDER_MATRIX.md), [research](WEB_COMICS_RESEARCH.md), [metadata](WEB_COMICS_METADATA_SOURCES.md), [adapter guide](WEB_COMICS_ADAPTER_GUIDE.md) and [structured backlog](WEB_COMICS_ROADMAP.md).

## Implemented integration

Follow-up: [Comics metadata search](COMICS_METADATA_SEARCH.md) now combines issue-first Comic Vine/GCD/Metron catalog results with Wikidata and configured Taddy. `comic_metadata.py` retains source IDs, field provenance, cover editions and download alternatives; run/year/number conflicts prevent blind title merging. The GUI adds selected issues through existing Watched/profile/graph logic. Issue downloads use all applicable approved connectors and reject trades, packs and known conflicting runs. Catalog-only series without a supported release watcher are explained in the UI rather than treated as RSS feeds. Credentialed live verification, bounded load-more, richer selected-issue details and provider-neutral series watching have explicit backlog entries in that report.

- `media_intake/webcomics/`: models, additive SQLite migration, adapter/metadata registries, resolver, feed/static/platform extraction, chapters, polite HTTP/cache/health, incremental watch state, original-byte CBZ/provenance manifests, library/detail UI and persisted manual overrides.
- Shared `core.py`, `serial.py`, `watched.py`, `gui.py`, `__main__.py`: reuse queue/downloader/scheduler/feed parsing/UI worker conventions; CLI resolution/list/poll; indexed watched queue lookup; bounded book work; first-class tab.
- `book_batch.py`: multi-series import with validation, profile assignment, per-series outcomes, duplicate reuse and future scheduling. Ctrl/Shift selection in book results leads to a preview and one background batch.
- `file_hosts.py`: independent registry for public Pixeldrain/MediaFire/Dropbox file resolution, used by GetComics and generic detail connectors. Bounded concurrent source searches rank exact matches first. Comic issue watches try alternate hosts; successful GetComics release identities suppress duplicate mirrors.

The existing watch/profile helper referenced undefined batch variables; it now returns normally. The watched video view also referenced add-dialog locals that did not exist during refresh; the regression is fixed and tested. No external source approval switches were changed.

## Validation and isolated test data

Comics metadata follow-up: **384 tests passed** in 297.498 seconds, recorded in `build/comics-metadata-final-tests.log`. The 45 focused metadata tests also passed after the final queue handoff refinement: issue downloads reuse `enqueue_for_watch`/IRC watch associations and preserve selected catalog IDs/provenance in ordinary queued-file metadata. Live Wikidata evidence is [research/comics-metadata-live-probes.json](research/comics-metadata-live-probes.json). Comic Vine/Metron live calls were not tested without credentials; GCD's anonymous request timed out. The [metadata report](COMICS_METADATA_SEARCH.md) distinguishes fixtures from live verification and explains setup and remaining coverage limits.

Webcomics foundation validation: **339 tests passed** in 260.996 seconds with `python -m unittest discover -s tests -q`. Expected fixture failures (HTTP 500 and an offline mirror) exercise recovery and appear in the log; they are not test failures. Test logs remain under ignored `build/final-webcomics-tests.log`; the durable public connector evidence is [research/download-connector-probes.json](research/download-connector-probes.json).

- Webcomics fixtures exercise static archives, WordPress multiple images, RSS/Atom, public vertical episodes, JS-only failure, structured metadata, recipes, pagination, chapter evidence, duplicate ordering, cursors, soft removals, revisions, parser collapse, conditional requests, HTTP errors/redirects/robots, manual overrides, removal/re-add, shared scheduler and per-entry failure isolation.
- Book stress adds eight series and 57 distinct books with overlaps and future releases, 49 real local EPUB transfers, a transient failure/restart, duplicate batches and a 2,000-item unrelated queue. Concurrent source tests use 20 book requests with a shared eight-worker maximum.
- GetComics load repeats a 1,000-link synthetic page 100 times with a 50-result cap; tests cover direct and wrapped mirrors, denied hosts, manual restrictions, best-host failure, completed-release reuse and checksum mismatch. Each static registered source connector performs a category-compatible local HTTP transfer. These are fixtures, not a high-volume crawl of a public server.
- Live xkcd resolution persisted 3,303 known entries (latest native ID 3,304; missing historical ID 404 excluded) in `build/webcomics-live-smoke` without fetching the backlist assets.
- Live book smoke downloaded six public-domain Gutenberg EPUBs for Alice, Sherlock Holmes and Oz series; repeated additions created no new books. Files and hashes are in `build/book-series-live-smoke/book-series-smoke.json`. Normal user watch/library data and device/email delivery were not used by the smoke.
- Live connector discovery checked 24 source keys. GetComics public search and wrapped Pixeldrain metadata resolved; the sampled main-server file returned 403. See [connector validation](DOWNLOAD_CONNECTORS_VALIDATION.md) for failures, empty samples and session-dependent limitations.
- A hidden Tk smoke verified the six tabs, Webcomics construction and extended result selection. Compile checks and whitespace checks passed.

## Windows launcher

The importer was opened. Desktop and Start Menu shortcuts named **Media Archive Importer (Latest)** launch the local `pythonw.exe run_gui.py` with the repository as working directory and a stable Windows app identity. Closing/reopening uses current local code; already running processes do not hot-reload. This does not pull Git updates, rebuild the packaged EXE or install an updater.

Taskbar pinning was not completed because the user pressed Escape during Windows UI control. That stopped computer-use operations. The manual step is to right-click the Latest shortcut and choose **Show more options → Pin to taskbar** where available, or pin the Latest running window. Avoid accidentally pinning the older packaged EXE. Existing user windows were not terminated.

## Repository state and next work

The checkout was already substantially modified with tracked and untracked book/media work before this pass. Those edits were preserved. No commit bundles them into this feature: review the current diff and stage owned changes deliberately. Shared integration edits cannot safely be committed wholesale without including earlier work.

Read the structured roadmap before adding a domain adapter. Highest remaining priorities: bounded WordPress REST/sitemap discovery; external metadata provider bridge; stronger image/CBR validation; cross-process source/job leases and disk-cache quotas; covers/pause/cancel/actionable health UI; public Tapas and broader WEBTOON pagination. Cross-site mirror reconciliation remains review-based. No browser renderer, site auto-inference, OPDS server, local reader or protected-source client is claimed complete.

Known bounds: 10 jobs/source per poll by default; 10,000 HTML entries, 500 feed items, 100 archive pages, 1,000 images/entry. Static archives can still require full conditional index parsing. Backlists take several checks; same-URL art changes without index evidence need a future bounded audit. WEBTOON newest-page catch-up can miss a gap larger than one list page until complete pagination support is expanded. RSS absence never means removal. Manual chapter corrections apply immediately and survive future extraction. Removing a watch retains its archive and re-adding reconnects the same source/history.

Operational details: the Webcomics **Check watches now** button uses the shared forced scan and checks all watched media. Downloads honor the existing rights settings; disabled sources can resolve metadata but cannot complete downloads until permitted through the existing settings. Archive-existing currently requires automatic archival to remain enabled while its backlog drains; independent one-time backlog and future-release controls are included in the UI backlog.
