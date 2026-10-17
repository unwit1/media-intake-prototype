# Torrent, Usenet, library and catalogue sources

Fifteen sources from the PirateBot source review are native Media Intake sources. They live in
`media_intake/connectors/` and show up in category search, the source-priority list, the queue and
**Settings → Sources**. This page covers what each one does, how to set it up, and exactly what was
verified.

## For people using the app

1. Open **Settings → Sources**. Every source is listed with its type, categories and status.
2. Sources that provide files (torrents, Usenet, direct downloads) are searched and downloaded only
   when their row in your local `rights-evidence.json` says `"approved_for_download": true`. They all
   start as NO. Use **Open rights file** and only switch on sources you are entitled to download from.
   Metadata sources (AniDB, WorldCat) need no approval.
3. Fill in what the source needs (an account, an API key, a mirror address). **Test search** runs one
   search and reports what happened in plain words.
4. Pick a category on **Search and add** and press **Search all connected sources**. The line under the
   search box says which of these sources will be searched and which are waiting for approval or setup.
   (Auto-detect leaves them out, so one search does not hit every site once per category.)
5. Releases of the same work are grouped under one row: the Format column shows what is on offer
   (`1080p, 720p · torrent/Usenet`) and the Source column how many releases were found. Double-click
   or **Choose release…** to pick one by quality, size and seeders. **Load more results** fetches the next
   page from sources that have one.
6. Queued releases download like everything else: progress, pause, resume, cancel, retries and source
   health on the Queue tab. A source that is down or blocked shows its own error and the rest still
   return results.

### What each source needs

| Source | Type | Categories | Needs |
| --- | --- | --- | --- |
| The Pirate Bay | Torrent index (apibay JSON API) | Videos, Anime, Games, Software, Books, Comics, Manga, Light novels | Nothing |
| 1337x | Torrent index (site pages) | Videos, Anime, Games, Software, Books | Usually Jackett + FlareSolverr: the site normally shows a browser check |
| RuTracker | Torrent tracker (site pages + public API) | Videos, Anime, Games, Software, Books, Comics, Manga | Free RuTracker account |
| Nyaa | Torrent index (RSS) | Anime, Manga, Light novels | Nothing |
| YTS | Movie torrents (JSON API) | Videos | Nothing (set a mirror address if the default is down) |
| EZTVx | TV torrents (site search, IMDb API) | Videos | Nothing; searching `tt0063350`-style IMDb IDs uses its API |
| FitGirl Repacks | PC game repacks (WordPress API) | Games | Nothing |
| NZBGeek | Usenet indexer (Newznab) | Videos, Anime, Books, Comics, Games, Software | API key, plus SABnzbd or NZBGet and a Usenet provider |
| DrunkenSlug | Usenet indexer (Newznab) | same as NZBGeek | API key (invite-only), plus SABnzbd or NZBGet |
| Z-Library | Ebook library (eAPI) | Books, Light novels | Account and your personal domain; daily limits apply |
| MagzDB | Magazine archive (site pages) | Books | Nothing |
| Vimm's Lair | Console game archive (site pages) | Games | Nothing |
| AniDB | Anime metadata | Anime | Nothing for titles; a registered API client name for details |
| WorldCat | Library catalogue metadata | Books, Light novels, Comics | OCLC WorldCat Search API key and secret; otherwise opens WorldCat in the browser |
| AIO Search | Browser search launcher | All | Nothing; off in category results by default |

**Torrents** use the built-in engine (`pip install -r requirements-torrent.txt`) or go to Gopeed.
Torrent sources with a Jackett indexer can be set to search **auto** (the site, then Jackett if the
site is blocked or down), **direct**, or **jackett** (always through your Jackett, set up in
Settings → Downloads).

**Usenet** releases are NZB files. Downloading the articles needs a Usenet provider account and a
newsreader. Add SABnzbd (address + API key) or NZBGet (address, username, password) under
Settings → Sources; the queue sends the NZB there and mirrors its progress, pause, resume and cancel.
Without one, set *Without a program* to `save` to keep the `.nzb` file in the library instead.

**Secrets** (passwords, API keys) are stored with Windows DPAPI. Elsewhere they last for the session;
set them as environment variables instead, named `MEDIA_INTAKE_<SOURCE>_<FIELD>`, for example
`MEDIA_INTAKE_NZBGEEK_API_KEY` or `MEDIA_INTAKE_Z_LIBRARY_PASSWORD`.

## Verification

**No live site was contacted for this work.** The development environment's network policy refused
connections to these sites, so live behaviour is unverified for every source. All search tests use
fixtures served over local HTTP that follow each site's documented response format (apibay JSON, Nyaa
RSS, YTS and EZTV JSON APIs, Newznab RSS, the WordPress REST API, Z-Library's eAPI, WorldCat Search
API v2, AniDB's title dump and HTTP API) or a known page layout (1337x, RuTracker, EZTVx, MagzDB,
Vimm's Lair). Downloads were exercised end to end against local stand-ins with deterministic test
files, never copyrighted works. A green fixture test proves the parser and the pipeline, not that the
live site still answers that way.

| Source | Search implementation | Search tests (all fixture) | Download | Completed transfer tests | GUI | Outstanding problems |
| --- | --- | --- | --- | --- | --- | --- |
| The Pirate Bay | Done: apibay `q.php`, category codes, magnet from info hash | Parse, sort, categories, "No results" placeholder, HTTP 404/429/5xx, bad JSON | Torrent | Real libtorrent transfer from a local seeder and tracker: search → queue → validated file; pause mid-transfer and fast-resume | Category search, single source, release picker, priority, settings | Live unverified. apibay returns at most 100 results, no paging |
| 1337x | Done: listing pages, paging, magnet read from the release page when the download starts; Jackett route | Listing, paging, magnet resolution, browser-check detection, automatic Jackett fallback | Torrent | Real libtorrent transfer with the magnet resolved from a release page at download time | As above, plus Jackett routing | Live unverified; the live site is usually behind Cloudflare, so Jackett + FlareSolverr is the practical route |
| RuTracker | Done: sign-in (cookie kept per session, re-sign-in when signed out), cp1251 search pages, forum-based category filter, paging; magnet from the public API, topic page as fallback | Sign-in, cookie use, cp1251, category filter, paging, session reuse, missing-account message, API hash → magnet | Torrent | Shares the torrent engine tested above; no RuTracker-specific transfer | As above | Live unverified. A sign-in CAPTCHA cannot be solved by the app (the message says to sign in once in a browser). Category filter is a forum-name keyword heuristic |
| Nyaa | Done: RSS with category and filter, paging | Hash, seeders, size, trusted flag, episode/quality/group parsing, gzip | Torrent | Shares the torrent engine | As above | Live unverified, including whether RSS honours the `p` page parameter |
| YTS | Done: `list_movies.json`, one row per quality, paging by movie count | Parse, paging, non-video category returns nothing | Torrent | Shares the torrent engine | As above | Live unverified; default `yts.mx` address may be out of date (set a mirror) |
| EZTVx | Done: name search page; IMDb ID search through `get-torrents` API with paging | Both paths, season/episode parsing | Torrent | Shares the torrent engine | As above | Live unverified; the name-search page parser assumes the long-standing EZTV layout |
| FitGirl Repacks | Done: WordPress REST search with paging, magnet, repack and original size, genres | Parse, skipping non-release posts, paging header, past-last-page 400 | Torrent (magnet) | Shares the torrent engine | As above | Live unverified. Direct-download mirrors in posts are not used, only the magnet |
| NZBGeek | Done: Newznab search, categories, offset paging, API key hidden from labels | Parse, totals, error codes (bad key, limits), missing key makes no request | NZB to SABnzbd/NZBGet, or save the .nzb | Fake SABnzbd: send, progress, completion, pause → resume (same job), cancel → deleted there, failed repair, indexer limit, no newsreader; fake NZBGet JSON-RPC to completion; saved .nzb | As above, plus newsreader settings and test | Live unverified; needs a paid membership to try. Completed jobs stay in the newsreader's folder; the queue records its largest file when that folder is on this computer, otherwise the folder path in the status |
| DrunkenSlug | Done: same Newznab client | Error-code handling | Same as NZBGeek | Same code path as NZBGeek | As above | Live unverified; invite-only |
| Z-Library | Done: eAPI sign-in or saved user ID/key, search with paging, supported formats only | Sign-in, cookies, format filtering, paging, missing-domain message | Direct file; link asked for at download time | Local server: complete and SHA-256 validated EPUB; interrupted transfer resumed with a fresh link and a byte range; HTML page instead of a book rejected; daily limit and unsupported format explained; no approval → no request; same result queued twice → one item | As above | Live unverified. The eAPI is unofficial and changes; domains are personal and short-lived |
| MagzDB | Done: title search → magazine page → issue rows | Parse | Direct file (issue page → file link) | PDF downloaded and validated; issue with no file explained | As above | Live unverified, and the `/makelist`, `/j/`, `/num/` paths are from the site's historical layout. The site has had long outages |
| Vimm's Lair | Done: vault search, platform and region, download form | Parse | Direct file with the page as Referer | Archive downloaded only when the Referer is sent; "downloads disabled" titles explained | As above | Live unverified. The live form may require POST; the app sends GET with `mediaId` |
| AniDB | Done: daily title dump (cached, fetched at most once a day) with ranking; details for the top 3 through the HTTP API (2-second spacing, cached a week) | Matching, ranking, dump caching, details, cached details | None (metadata) | Not applicable | Metadata in Anime category search and single source | Live unverified. Details need a client name registered at anidb.net |
| WorldCat | Done: Search API v2 with client-credential tokens, paging; browser link without credentials | Token use, record parsing (people and organisations), browser fallback | None (metadata) | Not applicable | Metadata in Books, Light novels and Comics | Live unverified; needs OCLC credentials for in-app results |
| AIO Search | Browser link only: the site runs other sites' searches in a browser page | Link building | None (discovery) | Not applicable | Single source; category row off by default | The `?q=` search address is unverified; it is editable in Settings → Sources |

### Cross-cutting behaviour that is tested

- Approval: new file sources start as NO in fresh and upgraded rights files; unapproved, turned-off and
  unconfigured sources are skipped with a reason and never contacted; without a rights file they fail closed.
- Isolation: a source returning errors, a source that hangs past the time limit, and a source whose
  parser raises each produce one error while the other sources' results are returned.
- Duplicates: the same torrent on several sites (same info hash) is one release listing every site;
  releases group under the work with its catalogue metadata (the 1968 film and the 1990 remake stay apart).
- Paging: Load more adds the next page to the existing groups.
- Settings persistence, secrets never written as plain settings, environment-variable secrets, and indexer
  and Jackett API keys removed from queued links (added back only when the link is fetched).
- GUI: Settings → Sources list and save, Jackett routing, single-source search refused until approved,
  grouped results, release picker queueing a magnet, category info line, and every new action in the
  app-wide smoke test.

Test files: `tests/test_connectors_search.py` (31), `tests/test_connectors_integration.py` (16),
`tests/test_connectors_downloads.py` (17, torrent cases skip without libtorrent),
`tests/test_sources_ui.py` (10), `tests/test_source_candidate_audit.py` (5). Fixtures are in
`tests/fixtures/connectors/`; `tests/connector_site.py` is the local stand-in site.

To check a source against the live site yourself, enter its settings and use **Test search** in
Settings → Sources, then queue a release of a work you are entitled to download.

## Handoff

- **Code:** `media_intake/connectors/` (`__init__.py` registry, settings, parallel search, download
  link resolution; `base.py` HTTP and plain-language errors; `releases.py` release-name parsing, magnets,
  duplicate merging; `torrent_sites.py`, `usenet.py`, `libraries.py`, `catalogs.py`),
  `media_intake/sources_ui.py` (Settings → Sources, release picker, result summaries).
- **Hooks into existing code:** `discovery.search_media_category` (a "more sources" task when the GUI
  passes `source_configs`), `discovery.search_more_connector_results`, `core._download` (link
  resolution, `.nzb` items, per-source request headers), `core.validate_item` (`.nzb`),
  `rights_evidence.DOWNLOAD_SOURCES`, `watched._approved_source_record` (fail closed), `gui.py` search,
  result columns, queueing, Load more and the Settings notebook.
- **Not done:** live verification of every source; automatic downloads from these sources for Watched
  items (they are searched and queued by hand); moving finished Usenet jobs into the library folder (a local job's main file is recorded);
  FitGirl direct-download mirrors.
- **Next steps:** run Test search for each source on a normal connection and record the outcome here;
  adjust any parser whose live page differs from its fixture (add the live page as a new fixture).
