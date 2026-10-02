# Webcomics architecture and implementation boundary

## Integration

`core.connect()` applies additive version-1 tables without dropping/changing existing media records. `watched.process_due_watches()` dispatches only webcomic watches bearing the new `metadata.webcomics` marker to the engine. Existing comic/manga/webtoon/feed watches remain on their original path. The GUI's existing worker, polling timer, settings/data-root conventions and Queue tab are reused. `core.download()` routes marked serial queue records to the webcomic asset worker after the existing rights checks.

The first-class Webcomics tab provides URL/title input, adapter selection, recipe loading, baseline versus archive-existing choice, automatic future archival, a library status table, a paginated chapter/entry/image tree, watch settings, source state, metadata evidence, recent events and persistent chapter overrides. Covers, reader progress, a local reader and a fully styled provenance editor are backlog features. This is a working first ingestion pass, not complete platform coverage.

## Normalized records

`models.py` defines Series, Volume, Chapter, Entry, Asset, Provenance and Snapshot dataclasses. Creator credits carry roles rather than flattening writer/artist/author. Series metadata includes alternate titles, tags, status/language/dates, homepage/source URL, cover, ratings/schedule, external IDs, copyright/license, field-level provenance and extraction version. Entries have stable source IDs, optional ordinal/number/date/chapter, type, transcript/notes, revision and ordered assets. Assets retain requested/final URL evidence, original media type, optional dimensions, checksum, archive member and retrieval time.

The database separates canonical series from sources. A source owns its adapter/config/cursor/health and entry numbering. A future reconciliation UI can attach several sources to one canonical series without overwriting per-source entries. Current exact canonical URL reuse is implemented; fuzzy title+creator matching returns evidence for review and never auto-merges. Cross-source edition equivalence is not yet persisted.

Hierarchy is optional: Series → optional Volume → optional Chapter/Arc → Entry/Episode → Asset. Chapter membership is an entry reference, never a required fake chapter. `SOURCE_DECLARED`, `INFERRED` and `MANUAL` remain distinguishable, with confidence/evidence. Source-declared archive organization wins over title inference. Episode numbers alone do not create chapters. Explicit boundary phrases and special sections can form candidates; broader TOC/breadcrumb/external metadata interpretation remains an extension point.

## Extractor and metadata boundaries

`AdapterRegistry` dispatches probe scores and stable adapter IDs, never a giant domain conditional. Adapters expose `probe`, canonicalization, series resolution, metadata, entry/chapter enumeration, latest entry, cursor updates, lazy asset resolution, capabilities and health metrics. Python adapters are registered explicitly; recipes are validated data, not executable scripts or remote plugin downloads. Registry replacement/configuration permits site extensions without changing scheduler/core logic.

`metadata.py` separately registers Schema.org and OpenGraph providers, stores competing field candidates and selected evidence, and supports locks. Feed/platform metadata uses the same candidates. Existing external catalogs are documented for later lookup/matching integration; this pass does not contact every metadata service automatically or flatten uncertain identities. Structured cover images are metadata, not guaranteed content assets.

`serial.parse_feed()` was extracted from the existing fetch path so webcomic HTTP caching can reuse the same RSS/Atom/JSON/3S semantics without monkey-patching global network functions. Feed windows are partial; absent old items never generate removals.

## Persistence and update protocol

`webcomic_series`, `sources`, `entries`, `chapters`, `volumes`, `assets`, `overrides`, `events` and `http_cache` are additive tables. Unique `(source, entry_key)` identity prevents duplicate URLs/IDs within a source; queue identities include source, entry and revision. Unknown-backlist entries are baseline `known` unless archive-existing is chosen. New/revised entries become `pending`, then `queued`, then `archived`. A ten-entry default batch drains on subsequent checks; no forgotten overflow after advancing a cursor.

Network work completes before index state is applied. Failed extraction does not advance the successful cursor. Durable fields include latest source entry, canonical ordinal, publication timestamp, index fingerprint, HTTP validators, feed URL, adapter version, success/failure timestamps and failure count. Publication timestamps are ordering evidence rather than the sole update identity, so backdated additions can still be detected.

Events cover baseline/new/updated/renamed/removed/restored entries, chapter additions/reassignments, metadata changes, layout changes, unreachable sources, suspect parsers and archived revisions. Removed entries are soft flags/evidence only; files and records are retained. Removal inference runs only for explicitly complete snapshots, with a count-collapse guard. Zero or a drop below half of a previously substantial complete index raises `PARSER_SUSPECTED_BROKEN`. The GUI retains errors and history. Same-URL artwork changes that are absent from an index are not yet automatically rechecked; a bounded asset-revision audit is in P2.

Metadata and asset resolution are separate. Lazy historical xkcd records initially have placeholder titles and source URLs until archived; this avoids one API request per old strip when merely starting a watch. Generic static archives still conditionally fetch their index each check, while xkcd checks latest JSON and WEBTOON polls only its newest list page after initial discovery. Feeds with validators return cached bodies on 304.

## HTTP and archival policy

Per-domain serialization and minimum intervals are shared across client instances. Requests have response/node/depth/page/entry/image/byte budgets. HTTP errors are classified, selected transient errors retry with bounded exponential waits, `Retry-After` can defer long waits to the scheduler, robots rules and crawl delay are respected, and conditional bodies/validators are stored in SQLite. User agent and interval are configurable. No browser runtime is spawned, no credentials are extracted and no access controls are bypassed.

Archives are one CBZ per entry/revision with ZIP_STORED original image bytes, `ComicInfo.xml` and `manifest.json`. Manifests contain source adapter/version, canonical metadata, entry, chapter evidence, original/final asset URLs, ordered checksums and timestamps. Archive member names are generated, never trusted remote filenames. Atomic temp-file replacement precedes database completion; failed jobs retain queue error state and do not block later entries in the same batch. Original revisions are retained by distinct filenames; asset history is revision-keyed. EPUB/PDF/folder/OPDS exports are downstream derivatives, not the normalized model.

## Scaling and known limits

Source-index uniqueness and pending indexes avoid duplicate work; download batches and bounded archive pagination avoid unbounded transfer bursts. Shared watches use the existing timer. The book worker pool now caps outstanding work at eight as well as four active workers. Queue lookup uses SQLite JSON expression indexes instead of decoding all items for every book.

For thousands of sources, further work is needed: durable robots caching (process-wide TTL is implemented), leases across processes, scheduler budgets/domain batching, event retention, disk-cache quotas, paged streaming snapshots, a content-addressed asset store, and bounded browser services. Current HTML indexes are materialized up to 10,000 entries, feeds up to 500, recipes up to 100 archive pages, images up to 1,000 per entry; these are explicit practical bounds, not infinite coverage. The shared GUI watch scan remains sequential for webcomics. See the structured roadmap for each dependency and acceptance test.
