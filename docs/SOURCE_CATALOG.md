# Source catalog and headless workflow parity

Handoff for the shared source identities (`media_intake/source_catalog.py`,
`media_intake/data/source_catalog.json`) and the shared workspace setup
(`media_intake/workspace.py`).

## Why

Source recognition lived in several hard-coded host lists, and the desktop window
was the only entry point that prepared a data folder. A queue item, a health
record or a download-manager task could therefore name the same source three
ways, and a command-line or scheduled run on a clean `--root` behaved
differently from the window. Both now go through one catalog and one setup
function.

## Source identities

One record per source: canonical `source_key`, display name, coverage, the
domains and names that resolve to it, the identities it must **not** be confused
with, and the existing Media Intake adapters it maps to. The domain values are
fixture routing identifiers for recognition. They are not statements about
current availability.

| Catalog ID | Source | Domains recognised | Coverage | Existing Media Intake support |
| --- | --- | --- | --- | --- |
| `yts` | YTS | `yts.mx`, `yts.bz` | Film | none; identity only |
| `the-pirate-bay` | The Pirate Bay | `thepiratebay.org` | General index | none; identity only |
| `nyaa` | Nyaa | `nyaa.si` | Anime | none; identity only |
| `fitgirl-repacks` | FitGirl Repacks | `fitgirl-repacks.site` | Games | none; identity only |
| `rutracker` | RuTracker | `rutracker.org` | Mixed media | none; identity only |
| `mangadex` | MangaDex | `mangadex.org` | Manga / comics | `comics.search_mangadex`, `comics.list_mangadex_chapters` |
| `pixeldrain` | Pixeldrain | `pixeldrain.com` | File hosting | `file_hosts.Pixeldrain` |

Records come from the PirateBot catalog (`data/sites.jsonl`). Existing
integrations are referenced, not copied: `source_catalog.resolve_adapters()`
returns the real callables, and a test fails if a reference goes stale.

### How names resolve

* A domain resolves when it equals a listed domain or is a subdomain of one
  (`www.yts.bz`, `cdn.nyaa.si`). `notyts.bz`, `yts.bz.example.net` and `nyaa.eu`
  do not.
* A name resolves on an exact match of the id, display name or a documented
  alias, ignoring case, spaces and punctuation (`The Pirate Bay`, `ThePirateBay`
  and `the-pirate-bay` are one name).
* `yts.mx` and `yts.bz` are the same record: the catalog lists two dated
  domains and asserts neither is current.
* Names listed under `distinct_identities` never resolve: `YIFY` (the original
  YIFY/YTS group is a separate historical identity) and `NyaaTorrents` (the
  pre-2017 service is separate from nyaa.si).
* Catalog validation rejects a domain, id, source key or name claimed by two
  records, and a "distinct" name that is also a known name.

## Where it is used

| Workflow | What changed |
| --- | --- |
| Source key for any item (`rights_evidence.source_key_for_item`) | Catalog hosts resolve to the catalog key; an alias used as an explicit `source_key` is canonicalised; a magnet link or a file on a mirror uses its `source`, `site` or `provider` to find the identity. Every key the existing integrations produced is unchanged. |
| Queue (`core.enqueue`, `enqueue_for_watch`, CLI `add`, `add-json`) | Items from catalogued sources carry `source_key`, `source_name` and a `dedupe_key`. |
| Duplicates | The same torrent reached through `yts.mx` and `yts.bz`, or a magnet written with other casing, a base32 hash or different trackers, reuses one row. The new URL is kept in `alternate_urls`. Rows saved before these fields existed are matched too. |
| Source health | History is stored and read under the canonical key. Older rows saved under an alias merge on read; nothing is migrated or deleted. |
| External download managers | Gopeed tasks get a `source_key` label when the item has a catalogued source. Other items keep their existing labels. |
| Watched items | Watch scans queue through the same functions, so they get the same identity and de-duplication. |

## Headless workflows

`workspace.ensure_workspace(root)` is the one first-run setup. The window, the
CLI and scheduled runs all call it, so a clean `--root` and a long-used one end
in the same state (database and migrations, the folder layout the window
creates, the media-graph change cursor, the rotating log). Running `python -m
media_intake --root <folder> list` on a new folder prepares it the same way
opening the window does.

`python -m media_intake --root <folder> process-watches [--force] [--library
<folder>]` checks every due watch once and prints JSON (`{"library": ..., "checked": N}`).
It is meant for a scheduler. It uses `workspace.watch_source_options()`, which
builds the per-source options from the folder's saved settings, the same values a
scan in the window uses when nothing has been edited. API credentials still come
from the environment and are never stored.

The window now calls the same helpers for logging, the IRC source list and the
feed flags. `tests/test_workspace_headless.py` compares the window's options with
the headless ones for a clean and a configured folder. It was checked to fail when
the headless defaults or the IRC import change; the window side is covered only
through the helpers it shares, since Tk was unavailable to run its own tests.

Defaults that differ on purpose and are unchanged: the window uses
`~/MediaIntakeData` (beside the executable in a frozen build), the CLI uses
`./media-intake-data`. Pass `--root` to use the same folder.

## Intentional behaviour changes

* `pixeldrain.com` and `mangadex.org` URLs now get the source keys `pixeldrain`
  and `mangadex` instead of the generic `direct_file_url`, so their health
  history is separate from other direct files.
* Queue items from catalogued sources have three extra metadata fields
  (`source_name`, `dedupe_key`, `alternate_urls`).
* Enqueueing an item that is the same torrent or catalog file under another
  spelling updates the existing row instead of adding a second one.
* The first CLI command that opens a database on a new `--root` now also creates
  the log folder and the window's folder layout.
* New CLI command `process-watches`.

## Limitations

* **Identity only (no search or detail parser):** YTS, The Pirate Bay, Nyaa,
  FitGirl Repacks and RuTracker. Media Intake recognises them, names them
  consistently and queues magnet and `.torrent` items attributed to them through
  the existing torrent and download-manager path. It cannot search them, read
  their pages or follow their detail links, and nothing in the repository
  documents their current entry points. The catalog records this per source
  (`limitation`), and a test requires it for each.
* Domains in the catalog are the ones in the reference records. A source that
  moves to a new domain is not recognised until the catalog lists it. This is
  deliberate: a similar-looking domain is never assumed to be the same service.
* Magnet links carry no host, so their identity comes from the `source`, `site`
  or `provider` field the discovering workflow sets. A bare magnet with none of
  them stays `direct_file_url`.
* The window's own tests need Tk and could not be run in the environment this was
  built in; the parity test covers the settings and IRC logic the window shares.
* `test_book_batch.py::test_two_connections_share_one_batch_without_double_processing`
  fails occasionally with a SQLite "database is locked" error (3 of 50 isolated
  runs, on the original code and on this branch alike).

## Tests

`tests/test_source_catalog.py` (records, aliases, lookalikes, validation, key
resolution), `tests/test_source_queue_integration.py` (identity, duplicates,
older rows, source health, download-manager labels),
`tests/test_workspace_headless.py` (clean, existing and older workspaces; window
and headless parity; scheduled scans, including a failing download). Fixtures:
`tests/fixtures/source_catalog/queue_items.json`.
