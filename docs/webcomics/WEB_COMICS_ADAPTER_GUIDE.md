# Adding a source adapter or recipe

No domain branches belong in `core.py`, the scheduler or downloader. Implement an `Adapter` or an `HtmlAdapter(Recipe(...))`, register it in `default_registry()` (or supply a registry to `resolve`), and add focused fixtures/tests. Independent deployment of executable third-party plugins is future work; local JSON recipes already avoid core rewrites.

## Declarative recipe

Save one recipe object as JSON and load it from the Webcomics tab or CLI:

```json
{
  "id": "creator-example",
  "version": "1",
  "domains": ["comic.example"],
  "title_selector": "h1",
  "archive_url": "https://comic.example/archive/",
  "archive_selector": ".comic-archive a",
  "image_selector": "#comic img",
  "chapter_selector": "h2,h3",
  "date_selector": "time",
  "next_selector": "a.archive-next",
  "order": "source",
  "max_pages": 20,
  "complete": false
}
```

Supported selectors: tags, `.class`, `#id`, combined tag/class/ID, descendants, comma-separated alternatives. Attribute/pseudo selectors and arbitrary expressions are rejected. This is deliberately a documented subset, not a complete CSS engine. Images use lazy attributes before `src` and preserve DOM order while deduplicating identical image URLs. Archive links are same-origin, pagination is bounded and loop-checked. `data-entry-id`, `data-chapter` and `data-published` are optional explicit evidence. One enclosing archive section with one heading provides chapter evidence.

Ordering: `source` preserves creator index order; `reverse` flips newest-first lists; `natural` orders numeric titles. Never assume publication dates establish reading order when dates are missing or backdated. Set `complete: true` only after verifying the index covers all entries and pagination reaches the end; this enables soft removal evidence and never deletes local archives.

```powershell
python -m media_intake --root C:\MediaIntakeTest add-webcomic https://comic.example/archive/ --recipe .\recipe.json --archive-existing
python -m media_intake --root C:\MediaIntakeTest list-webcomics
python -m media_intake --root C:\MediaIntakeTest poll-webcomics --force
```

The baseline default saves index metadata and watches future entries. `--archive-existing` queues old entries in bounded batches. Existing source/per-work settings still apply when downloads execute. CLI and GUI use the same SQLite tables and jobs.

## Specialized adapters

An adapter's stable `id` and version identify its extraction contract. `probe(url,response)` returns confidence without unnecessary network work. `canonicalize_url` must retain source identity parameters; it may map an episode URL to a verified parent series. `resolve_series` / `get_updates_since` returns a `Snapshot` containing normalized records, explicit completeness, cursor evidence and health metrics. Empty incremental results are valid only when positively declared incremental; empty HTML indexes are parser failures.

`resolve_assets(entry,client,config)` loads only the selected entry and returns ordered original public images. All network requests must go through the shared client; do not instantiate unmanaged browser/network clients. Separate official public APIs from undocumented endpoints and advertise capabilities accurately. Do not solve challenges, replay unauthorized private tokens, or retrieve locked episodes.

Source entry identity must be stable across renaming, feed rotation and archive reorder. Use native IDs when available; otherwise canonical source URLs. Ordinal and publication date are not identities. Prefer complete indexes on initial discovery plus cheap latest/feed/API calls during watching. If the latest page can miss more than one poll window, implement bounded catch-up to a known ID and preserve continuation state.

Emit explicit creator organization as `SOURCE_DECLARED`; inferred boundaries carry lower confidence and evidence. A per-entry manual override remains authoritative. Never turn every episode into a chapter or fold every non-story post into the main narrative.

## Required validation and attribution

Fixtures are small synthetic layouts in `tests/fixtures/webcomics`; they contain no copied comic art, credentials or production pages. Add archive/list, entry/images, pagination and changed-layout cases. Test duplicate IDs/URLs, date disorder, redirects, HTTP 404/429/500, missing selectors, revisions, zero-result regression and durable cursor restart. Include a shared queue integration check, not just a parser test.

```powershell
python -m unittest discover -s tests -p test_webcomics.py -v
python -m unittest discover -s tests -p test_book_batch.py -v
python -m unittest discover -s tests -q
```

If borrowing implementation, first inspect the exact file license, pin a commit, and update the reuse ledger with source/version/files/modifications/attribution. Repository license detection alone is not sufficient. This pass copied no upstream implementation. Use the research and provider matrix before adding another downloader dependency.
