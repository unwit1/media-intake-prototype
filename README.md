# Media Intake prototype

A local Python 3.10+ worker for finding, queuing, downloading, renaming, and importing ebook files. Most features use the Python standard library; watched fanfiction downloads use the optional FanFicFare package. Queue and provenance are stored in SQLite under `--root`; downloaded files are checked, hashed, and named from title, author, and optional series. It can import one completed file into Calibre with `calibredb add`.

## Quick start

The recommended desktop entry point is the Tkinter GUI. Run `python -m media_intake gui` from the project folder, or use the Windows portable executable produced by the repository workflow.

From this directory:

```bash
python -m media_intake search-openlibrary "Alice in Wonderland" --limit 2 > results.json
python -m media_intake add-json results.json --index 0
python -m media_intake list
python -m media_intake download 1
python -m media_intake import-calibre 1 --library "/path/to/Calibre Library"
```

The GUI provides source search, result review, direct URL intake, queue status and transfer progress, downloads, and a persistent **Watched** tab. Network work runs in the background so the window remains responsive. It uses the same SQLite queue and download validation as the command line commands.

In **Settings → IRC sources**, add one row per server/channel/nickname, assign the media categories it can search, and move rows to set IRC fallback order. A later IRC source is queried only when higher-priority sources return no matching files. Existing JSON IRC config paths are imported into this list automatically; passwords should be supplied through an environment variable. The Queue tab shows percentages when a file size is known, and current phase/bytes for transfers whose size is not known in advance.

GUI errors and background watch failures are saved in a rotating diagnostics log at `<data folder>/logs/media-intake.log` (four 2 MB backups). Use **Open logs** in the toolbar to inspect it. Download and watch errors are also retained on their queue/watch rows so they can be retried and diagnosed later.

### Watched books, series, authors, manga, and comics

In the **Watched** tab, use **Goodreads** to search books, series, and authors. Use **AniList** for manga metadata, **MangaDex** for tracked chapter releases, or **Taddy** to search comic, manga, and webtoon metadata backed by a public 3S feed. Taddy requires an API user ID and key from [its GraphQL API](https://taddy.org/developers/intro-to-taddy-graphql-api); enter them when prompted or set `TADDY_USER_ID` and `TADDY_API_KEY` for the running app. Credentials are not stored in the library database. Taddy can only track series that expose an SSS/3S feed; it is not a catalog of every webcomic on the internet. For other sites, enter a public RSS, Atom, or JSON Feed and the CSS selector containing the chapter images. Metadata URLs, feed URLs, and manually entered titles are also supported.

A saved library folder and the existing connected searches apply to comic watches too. They search Open Library, LibGen, Anna's Archive, OPDS feeds, local JSON catalogs, and IRC for CBZ files. MangaDex chapter checks request English, safe-rated releases and select one current translation per chapter number. Every new release is saved as a CBZ with a `ComicInfo.xml` containing available source and creator credits. Taddy's public SSS issue images are packaged as CBZ files; issues with an exclusive-release date are ignored until that date. Other sites can use RSS, Atom, or JSON Feed; the feed items link to chapter pages and the selector identifies the page image container.

Choose **Web serial** for a public RSS, Atom, or JSON Feed. The app establishes a baseline on the first check, then searches connected ebook sources and saves new chapter pages as EPUBs in the saved library folder. The feed remains the release index, so chapter pages must expose readable text to the configured selector.

Choose **Fanfiction** and enter a story URL to use [FanFicFare](https://github.com/JimmXinu/FanFicFare). It supports more than 100 sites, including AO3, SpaceBattles, Questionable Questing, Sufficient Velocity, and Fiction.Live. The first check downloads the story to one EPUB; later checks update that EPUB with newly published chapters. The open-source GUI run needs the optional dependency installed with `python -m pip install -r requirements-fanfiction.txt`. The portable Windows build bundles the FanFicFare command. Downloads run non-interactively without browser cookies, browser cache, or Cloudflare workarounds; stories requiring a login, age confirmation, or blocked by a site may fail and will show the source error in Watched and in the local log.

The first chapter-feed check establishes a baseline so adding an existing series does not download the whole back catalog. Later releases are downloaded automatically, up to 10 new files or chapters per watch check. Checks run while the GUI is open and overdue checks resume on the next start. A book with a future release date is marked **Unreleased** and is first searched on that date. FMHY is a discovery directory rather than a direct-result connector, so its search links still need manual resolution.

Video episode feeds accept RSS, Atom, JSON Feed, iCalendar, and Schema.org JSON-LD `Event` / `ItemList` schedules. Publisher or studio schedule feeds also accept RSS/Atom announcements, iCalendar, a JSON schedule envelope, or JSON-LD event lists. JSON-LD nested `TVSeries` / `TVSeason` episode lists are supported too. Future dated events go to **Needs Review** on the first scan; accepting a premiere schedules the watch, while accepting an episode creates its child watch and release-day search. YouTube playlist API metadata also retains video duration and scheduled/actual live broadcast timestamps when available. JSON schedule feeds use `{"@type":"MediaSchedule","name":"Studio","events":[{"id":"event-1","title":"Show premiere","event_type":"premiere","start_date":"2027-10-17","url":"https://studio.example/news","description":"Season two premieres."}]}`. Event types include `episode`, `premiere`, `availability`, `renewal`, `cancellation`, `hiatus`, and `release_date_change`. JSON-LD screening events retain their event type for review; `PublicationEvent` and `BroadcastEvent` become availability candidates. `TVEpisode` entries retain season and episode numbers, publication dates, and linked video metadata.

For a Goodreads series or author, the app remembers the books already listed and checks the page daily for new entries. Open Library, Google Books, and Wikidata-backed series also refresh their explicit member lists daily. Newly listed works are added once and searched through the connected book sources; upcoming books wait for a supported release date. Series entries labeled as parts, volumes, or common formats such as paperback and Kindle edition appear in **Needs Review** instead of being queued automatically; accept them there if they are separate works. Goodreads may restrict or change public page markup, so a URL that cannot be read can still be added by entering its title and author; discovery requires a readable Goodreads page.

Catalog disagreements for a watched book's release date, lifecycle status, or series position appear in **Needs Review** with provider evidence. Use the canonical-choice buttons to apply a date or position to the watched book; future dates schedule the next search, and a chosen series position remains in place when catalogs refresh.

To inspect a title's continuity, select a watched item and click **View continuity and releases**. **Refresh catalog relationships** refreshes linked Wikidata, AniList, TMDB, TVMaze, IGDB, Steam, Open Library series, and RanobeDB series identifiers throughout the displayed relationship neighborhood (up to three hops and 100 entities). It records graph changes without queueing downloads or creating watches. TMDB and IGDB refreshes use the API keys configured in Settings. **Discover related** remains available to associate a matching Wikidata entity. Selecting a Goodreads series creates one series watch; its listed books are retained as sourced graph relationships instead of separate Watched rows. The series title itself is searched through its connected book sources. Release dates without a complete day are retained as metadata but do not schedule a search.

The **Search and add** tab groups searches by media category. Books search Goodreads, Open Library, Google Books, Wikidata, and configured book download connectors. Google Books and Open Library are metadata repositories; neither is treated as a download source. Explicit Google Books series names and positions can contribute ordered series members, and validated ISBNs help merge the same work across catalogs. Comics search GetComics and the configured comic mirror (subject to the local rights file), with Comic Vine and Taddy metadata when credentials are configured. Manga searches AniList, MangaDex, MangaUpdates, and configured Taddy. Anime searches AniList and Wikidata plus the Anime Tosho, Tokyo Insider, AnimeOut, NoobSubs, and Chauthanh download connectors when each has a YES entry in the local rights file. Videos search TMDB, TVMaze, and Wikidata; Games search IGDB, Steam, and Wikidata. TMDB and IGDB use the API keys configured in Settings. These are metadata sources; configured file connectors are searched separately according to the category's download-source priority. Internet Archive, Meawfy, and SoftArchive are available as download sources across all media categories, with file formats limited to the selected category. Web serial and light novel categories use their available catalogs. Fanfiction accepts a FanFicFare supported story URL because those sites do not share one text search catalog. Individual connector search remains available as an optional override.

Downloaded books appear in **Library**. In **Settings > Kindle**, choose **USB** or **Email** as the delivery method and optionally enable automatic delivery. USB copies to the selected Kindle Documents folder and verifies the file hash. For wireless delivery, add one row per Kindle device with its unique Send to Kindle address, then enter the shared SMTP server, sender address, and SMTP credentials; Amazon only accepts senders on the account's Approved Personal Document Email List. Configure the SMTP password with your mail provider's app-password option when needed; the app protects it with Windows DPAPI for the current Windows user. Email defaults to EPUB; PDF and TXT can also be attached, and Amazon converts EPUB. The optional AZW3 email format is experimental because Amazon does not list AZW3 among its supported personal document types. AZW3 files can also be sent by USB as-is; MOBI input needs the MOBI USB output selected or an EPUB/OPF source to convert to AZW3. Amazon allows up to 50 MB per email. SMTP acceptance is recorded as **Email sent; arrival unconfirmed**, since the app cannot see Amazon's processing or confirm that a document reached the Kindle library. Kindle may also request email verification. **Send selected to Kindle** uses the selected route. The portable build bundles the MIT-licensed Kindling converter for EPUB/OPF-to-AZW3 conversions; already-AZW3 files are copied directly. MOBI output remains available for older devices. Kindling requires EPUB or OPF input and cannot convert DRM-protected books.

The **Software** category searches SourceForge and GitHub releases for projects whose repository reports an identified open-source license. Those download connectors require separate YES entries in the local rights file. FOSShub is used as metadata because its catalog describes open-source projects while current product pages point to publisher downloads. The app limits software results to known package and archive formats. Other FOSS indexes on FMHY are curated directories without a compatible structured search or release endpoint, so they are not treated as file sources.

Additional approved file sources include MediaFireTrend, XDCC.eu, WarezOmen, MyAbandonware, OldGamesDownload, AbandonwareGames, GOG Revived, and AnkerGames. The first three are category-wide; the game archives are connected to **Games**. Their source rows are added to `rights-evidence.json` as NO. XDCC.eu search rows become IRC pack queue items. The Google CSE entry opens the supplied search engine in a browser because the app cannot retrieve its JavaScript-rendered results. Sites that return a Cloudflare challenge or an HTTP denial from the app are shown as browser-search links; the connector does not attempt to bypass those pages.

Use **RanobeDB** in the Watched tab to add a light-novel series. Its catalog supplies English volume release dates; the first check baselines released backlist volumes and creates watched entries for upcoming English releases. Each entry starts searching connected book sources on its listed English release date. The watch status distinguishes upcoming English releases, English releases already listed, and no English release listed. MangaUpdates can show whether a manga series is marked licensed in English and the listed English publishers; this is series-level licensing metadata, not a guarantee that a specific chapter or volume is available.

LNORI and NovelArchive search results provide metadata and catalog pages. If the corresponding source is approved in the local rights file, Watched follows direct EPUB, PDF, CBZ, and related file links on those pages. NovelArchive series URLs can also be watched as web serials; new chapter pages are gathered into EPUBs. File-host pages that do not expose a direct file link are left for manual review. For comic issue tracking, search **Comic Vine** and select a volume. Set `COMICVINE_API_KEY` in the app environment for daily schedule updates; a key entered in the UI is kept in memory for the current session only. Future issues become watched entries and are checked on their store date.

**GetComics** can be used by comic and issue watches. Its connector searches paginated result pages, reads matching release pages, follows direct “Main Server” download redirects, and downloads CBZ/CBR/PDF files into the saved library. Browser-oriented external file-host pages such as MediaFire are not treated as direct files. The connector flow is based on the search → release page → server link pattern used by [GetComics Downloader](https://github.com/UlucKaymak/getcomics-downloader); that CLI also leaves MediaFire links for the user to open. A separate HTTPS **Public-domain comic mirror base** remains available for custom mirrors.

### Local source approvals

Every data folder contains `rights-evidence.json`, the app's single local yes/no source approval file. The source entries for GetComics, LNORI, NovelArchive, Internet Archive, Meawfy, SoftArchive, Anime Tosho, Tokyo Insider, AnimeOut, NoobSubs, Chauthanh, SourceForge, GitHub FOSS releases, and other download connectors start with `"approved_for_download": false`. Set the exact JSON boolean to `true` for a source, and store or correct its evidence in that same file. The Watched scanner and queue downloader check this value before resolving or downloading files. A comic watch can also store a per-work yes/no decision from **Edit rights evidence**; a work-level NO pauses that comic even if a source is enabled. Use **Open rights file** in Watched to edit the source entries.

On Windows, use `py` in place of `python` if needed. Put `--root "C:\\Media Intake"` before the subcommand to choose where data lives. `search-openlibrary` uses Open Library's search API and Internet Archive's item metadata to find actual EPUB/PDF links; results are not added to the queue until selected. The live search and download were tested with *Alice's Adventures in Wonderland* from Internet Archive. Some item records will lack accessible ebook files.

## Website sources

The prototype supports these discovery paths:

| Source | Command | Status |
| --- | --- | --- |
| Open Library / Internet Archive | `search-openlibrary QUERY` | Live search and EPUB download tested |
| Internet Archive media files | Search and watched connectors | Category-aware metadata/file selection; fixture-tested, including watched download |
| FOSS software: SourceForge and GitHub | Software category search | OSI license-filtered project/release search; source approvals default to NO |
| FOSShub software catalog | Software category metadata | Structured metadata search; results link to publisher project pages |
| Meawfy and SoftArchive | Search and watched connectors | All-category HTML listing/detail adapters; fixture-tested through direct file download |
| MediaFireTrend, XDCC.eu, WarezOmen | Category search, watched and priority settings | YES-gated; XDCC.eu results map to IRC pack requests; challenge pages fall back to browser links |
| MyAbandonware, OldGamesDownload, AbandonwareGames, GOG Revived, AnkerGames | Games search, watched and priority settings | YES-gated; HTML detail-file resolution where available, browser fallback for denied or JS-only searches |
| FMHY Google CSE | Category-independent browser search | Opens the supplied engine with the query; result extraction requires browser review |
| OPDS catalogs, including Standard Ebooks when its feed is reachable | `search QUERY --opds FEED_URL` | Parser tested locally |
| Local JSON catalogs / Project Gutenberg direct links | `search QUERY --catalog examples/catalog.json`, or `add --url ...` | Direct downloads tested with three Gutenberg EPUBs |
| Anna's Archive | `search-site annas QUERY --base SITE_URL` | HTML adapter fixture tested; live site returned HTTP 403 in this workspace |
| Library Genesis | `search-site libgen QUERY --base SITE_URL` | Search, detail link resolution, and EPUB download tested on `libgen.gl`; other domains may differ |

The GUI also has a **Snahp forum threads** search source. Its adapter is limited to thread titles and same-site topic URLs; it discards post snippets, file-host URLs, and other links, and thread results cannot be queued as downloads. The public index identifies phpBB, with forum pages and topics linked through `viewforum.php` and `viewtopic.php?t=...`. The search page says guests cannot use search. A normal browser first showed Cloudflare's security verification and then loaded the public index; direct requests returned HTTP 403. The initial adapter expects phpBB's `search.php?keywords=...` results with `.topictitle` links. The **embedded Snahp browser** keeps its WebView2 profile under the app data folder. After manual sign-in and search, click **Import thread titles** in the page; it saves an HTML snapshot to the existing thread-only parser. The app does not automate sign-in or bypass a site challenge. A manual HTML file picker remains available.

Goodreads can also redirect or deny the app's public-page request. If that happens, the Goodreads lookup opens the corresponding page in your browser and preserves the selected name and URL. Save the search or author page as HTML and use **Import Goodreads HTML**; imported author metadata is stored as the baseline so old books are not treated as new releases.

## FMHY fallback directory

The [FMHY reading index](https://fmhy.net/reading) is a site directory, not a catalog of individual books. The included snapshot contains 31 named sites across its ebook, public-domain, PDF, and special-interest sections. The `fallback` command searches Open Library first, then any OPDS feeds and named adapters you configure. If no title match is found, it returns site-specific search links from that directory; it does not label those links as actual book matches or download files without resolving a detail page.

```bash
python -m media_intake fallback "An Obscure Book" --opds "https://your-opds.example/feed" --site annas --limit 15
python -m media_intake list-fmhy "An Obscure Book" --section "Public Domain" --limit 30
python -m media_intake resolve-site generic "https://a-listed-site.example/book-page" --title "An Obscure Book" --html saved-detail.html > links.json
python -m media_intake add-json links.json --index 0
python -m media_intake download 1
```

For a fresh complete directory, run `python -m media_intake refresh-fmhy --out fmhy-reading.json`, then add `--directory fmhy-reading.json` to `fallback` or `list-fmhy`. If the site blocks an automated request, save the reading page HTML in a browser and run `refresh-fmhy --html saved-reading.html --out fmhy-reading.json`. That parser scans all supported reading sections and retains the first site link and its numbered mirrors for each entry. Refreshing or browsing a directory does not make every linked site searchable automatically: sites with custom APIs, logins, browser challenges, or unusual download flows need individual adapters. The generic detail-page resolver can extract direct file links from any saved HTML page.

Site domains change. `--base` supplies a domain you have verified in your browser. Do not treat a result-page URL as a media file. To use a browser page that the worker cannot fetch, save its HTML to disk and run:

```bash
python -m media_intake search-site annas "Example Book" --base https://your-current-domain.example --html search.html
python -m media_intake resolve-site annas "https://your-current-domain.example/md5/RECORD_ID" --title "Example Book" --author "Author" --format .epub --html detail.html > links.json
python -m media_intake add-json links.json --index 0
python -m media_intake download 1
```

Use `libgen` instead of `annas` for its adapter. LibGen's currently tested `/index.php?req=` results include edition title, author, format, ISBN, and a detail link. The detail adapter extracts candidate direct file or download-route links. A link requiring a login, browser challenge, intermediary page, or changed page layout can fail. It does not evade those mechanisms. The saved-page parser is a starting integration point for the existing Firefox download assistant; it is not wired to that extension yet. `--format` is needed for an opaque download route without a file extension.

You can queue any direct downloadable file link with metadata:

```bash
python -m media_intake add --title "A Book" --author "An Author" --url "https://example.org/a-book.epub" --format .epub --source "https://example.org/book-page"
```

For an OPDS feed, use `python -m media_intake search "Jane Austen" --opds "https://your-catalog.example/opds" --pages 3`. This scans requested feed pages and follows Atom `next` links; it does not guess a provider-specific search endpoint. `examples/catalog.json` is illustrative and its example.org URL is not a real download.

## IRC / XDCC

The channel-search flow described in the [Reddit ebook IRC guide](https://www.reddit.com/r/Piracy/comments/hfil8c) is supported through `@search` and the ZIP listing returned by DCC. The included `examples/irc-search.json` points to the IRC Highway `#ebooks` channel; edit its server and channel for another network. Search results include the bot, filename, format, and a request command, and are only added to the queue after you select one:

```bash
python -m media_intake search-irc "Frankenstein" --config examples/irc-search.json > irc-results.json
python -m media_intake add-irc-result irc-results.json --index 0
python -m media_intake download 1
```

`search-irc` reads text listings within a bounded ZIP archive in memory and does not unpack archive paths. The selected result sends its `!Bot filename.epub` request in the channel and receives the matching bot's DCC file. The result parser recognizes EPUB, PDF, MOBI, AZW3, and CBZ lines. Results use the common `Author - Title.ext` naming convention, which can be corrected in the JSON before queuing. Network-specific response formats may need another parser; a search index is a list of offers, not proof of a complete or accurate edition.

In **Settings > IRC sources**, add a server and use **Connect & list channels** to load its public channel list. Check one or more channels; each becomes its own searchable, priority-ordered source. For UnderNet hosts, the picker limits its LIST request to the linked guide's `#Bookz` and `#ebooks` channels because a full-network LIST may disconnect clients. IRC networks with self-signed TLS certificates can use the optional **Allow unverified certificate** setting; this encrypts the connection but does not verify the server's identity. Keep certificate verification on when the server has a valid certificate. Edit `examples/irc-server.json` to specify your server, channel, nickname, bot, numeric pack, and book metadata. Then:

```bash
python -m media_intake add-irc examples/irc-server.json
python -m media_intake list
python -m media_intake download 1
```

The client connects to that server (TLS by default), joins the channel, sends the commands, waits for `DCC SEND` **from the configured bot**, and receives one file. The DCC file name is never used as a local path; the received extension must match `format`. The normal queue, hash, archive validation, retry, and Calibre import steps apply. Set `password_env` to the *name* of an environment variable if the IRC server needs a connection password; never put the password in the JSON file.

`commands` accepts 1–10 entries using `/msg TARGET TEXT`, `/notice TARGET TEXT`, and `/join CHANNEL`. `{bot}`, `{pack}`, `{request}`, and `{channel}` are replaced from that server's config, so different servers can use commands such as `/msg {bot} xdcc send #{pack}` or `/msg {channel} !get {pack}`. The default for numeric packs is the XDCC example. It supports ordinary active DCC SEND; reverse/passive DCC, resuming partial transfers, SASL, and CAP negotiation are not implemented. The IRC server can use TLS, but ordinary DCC file transfer is a direct unencrypted connection and exposes the receiver's IP address to the sending peer.

## Comics, manga, webtoons, web fiction, and fanfiction

For a direct EPUB, PDF, or CBZ file, use `add --url` as above. For chapter pages, use an ordered manifest. Supported `kind` values are `comic`, `manga`, `webcomic`, `webtoon`, `webfiction`, and `fanfiction`. The first four fetch chapter images and build a numbered CBZ; `webfiction` extracts text paragraphs from feed-linked pages and builds EPUBs. Watched fanfiction URLs use FanFicFare to create and update a single EPUB per story.

```bash
cp examples/serial.json my-serial.json
# Edit title, author, kind, selector, and ordered chapter URLs.
python -m media_intake add-serial my-serial.json
python -m media_intake download 1
python -m media_intake import-calibre 1 --library "/path/to/Calibre Library"
```

For sites publishing RSS, Atom, JSON Feed, or iCalendar chapter feeds, discover a draft manifest from either the feed URL or a creator homepage that advertises it. Review its chapter order and selector, then queue it:

```bash
python -m media_intake discover-feed "https://example.org/story/feed" --kind fanfiction > my-serial.json
python -m media_intake add-serial my-serial.json
```

The `selector` accepts comma-separated HTML tags, `.classes`, and `#ids`, such as `article,.chapter-content,#reader`. Text extraction reads paragraphs and headings within the selected container; image extraction uses `data-src`, `data-lazy-src`, `data-original`, `data-url`, `src`, and `srcset`. Configure chapter URLs explicitly for sites without an advertised feed. Browser-rendered chapters, account-only pages, scripts that populate content, complex pagination, and unusual image loaders need site-specific adapters. The example URLs are placeholders. Feed discovery reads the latest feed page and reverses its typical newest-first order; review it when a feed uses a different ordering. The prototype does not automatically crawl all chapters on an arbitrary site.

AniList offers a public GraphQL API for manga metadata and does not require authentication for public catalog queries ([AniList API guide](https://docs.anilist.co/guide/graphql/), [public API introduction](https://docs.anilist.co/guide/introduction)). MangaDex exposes public manga and chapter-feed endpoints; its own [API documentation and acceptable-use policy](https://gitlab.com/mangadex-pub/mangadex-api-docs) require crediting MangaDex and scanlation groups when chapters are presented, and prohibit ads or paid services using the API. The app keeps source and scanlation credits in each generated MangaDex CBZ. Comic Vine issue schedules require an API key and follow its non-commercial usage and attribution conditions ([Comic Vine API terms](https://comicvine.gamespot.com/api/)). RanobeDB provides light-novel book/series metadata, English-language release dates, and official retail links through its [public API](https://ranobedb.org/api/docs/v0); its v0 data is non-commercial and ODbL/Database Contents licensed. MangaUpdates supplies a series-level English licensing flag. Goodreads remains useful for books, while AniList and MangaDex provide structured manga catalog and chapter data.

## Queue behavior and development

`download` is explicit; search results never fetch themselves. Repeating `add` for the same URL preserves the queue entry. Repeating `download` reuses an existing completed file. If a named file already exists with different bytes, a short SHA-256 suffix is added. A failed download records its error and can be retried with `download ID`; interrupted transfers resume from saved progress. Invalid EPUB/CBZ archives and PDF headers are rejected and their partial data removed. Files land under `<root>/downloads/`.

Downloads use a built-in engine: multiple connections per file, resume after pauses, errors and restarts, pause/cancel, a speed limit, and retries. Shortlinks and file-host pages are resolved automatically. Magnet and `.torrent` links download natively with the optional `requirements-torrent.txt`. Jackett search and clipboard link capture are also built in. See [Downloads](docs/DOWNLOADS.md).

**Back up database…** (toolbar) saves a consistent copy of `intake.sqlite3`, which holds the queue, watch list, settings and follows, while the app runs. To restore, close the app and put the copy in the data folder as `intake.sqlite3`. **Export watch list (CSV)…** on the Watched tab writes every watched item with its series, release date and status.

`python tools/metadata_check.py` runs a live search for 50 well-known series across every category and reports missing titles, duplicate rows and edition clutter; `--more`, `--third` and `--fourth` run three further sets of 100. [Search quality](docs/SEARCH_QUALITY.md) catalogues every fix those checks led to.

`python -m unittest discover -s tests -v` runs local HTTP, source-parser, Calibre invocation, fake IRC/DCC, Goodreads saved-page, and mocked FanFicFare update tests. These tests do not contact live ebook/IRC/fanfiction servers. Calibre must be installed and `calibredb` on PATH for a real import.

## Syncing to a Windows PC

The repository is the shared source of truth. On the PC, install Git for Windows and Python 3, then run PowerShell from a checkout:

```powershell
git clone https://github.com/unwit1/media-intake-prototype.git "$env:USERPROFILE\GitHub\media-intake-prototype"
Set-Location "$env:USERPROFILE\GitHub\media-intake-prototype"
powershell -ExecutionPolicy Bypass -File .\tools\setup-pc.ps1
```

To pull later changes safely:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\sync-to-pc.ps1 -RunTests
```

The sync script fetches `origin/main` and only fast-forwards the local checkout, so it will stop instead of overwriting local commits. To sync automatically every 30 minutes, run `setup-pc.ps1 -ScheduledTask`; use `-IntervalMinutes` to change that interval. The GitHub Actions workflow runs the same test suite on Windows for every push and pull request.

## Portable Windows executable

Every push to `main` builds a single-file `MediaIntake.exe` on a Windows runner, uploads it as a workflow artifact, and updates the private `portable-latest` GitHub release. Download the executable directly from [the latest release](https://github.com/unwit1/media-intake-prototype/releases/latest/download/MediaIntake.exe); no Actions-page navigation is needed. Python and Git are not needed to run the executable. The portable executable stores its queue, logs, embedded-browser profile, and forum-page captures in a `MediaIntakeData` folder beside the EXE. The source checkout uses `%USERPROFILE%\MediaIntakeData` by default.

To build it locally on Windows instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build-portable.ps1 -Clean
.\dist\MediaIntake.exe
```

The build script uses PyInstaller and bundles FanFicFare and the small pywebview host. The embedded sign-in browser uses the machine's Evergreen WebView2 Runtime, which is normally installed on current Windows systems; the fixed Chromium runtime is not bundled because it adds more than 250 MB. The separate WebView2 profile and captured forum HTML live under the selected app data folder.

## Comics metadata search

In **Add Anything**, choose **Comics** and **All connected in category**. Search a title or `Batman (2016) #12`; the parenthesized year identifies the series run. Individual issues appear before series and trades. Matching metadata records and download mirrors share one result with provenance and alternates; conflicting runs, languages and collected editions stay separate.

GCD and Wikidata are attempted publicly; add Comic Vine and Metron credentials under **Settings → Metadata APIs** for their issue catalogs. `COMICVINE_API_KEY` and `METRON_API_TOKEN` also work in the launch environment. Taddy remains available when configured. Adding a selected issue watches only that issue, and its download search uses applicable approved connectors. See [coverage, verification and expansion backlog](docs/webcomics/COMICS_METADATA_SEARCH.md), including API limits and unverified live credentialed paths.

## Webcomics and bulk watched books

The **Webcomics** tab accepts public series, page or feed URLs. It resolves supported sources, keeps a durable archive index, watches new/revised entries through the existing scheduler, and archives original images into CBZ files with ComicInfo and provenance manifests. Starting a watch records the existing archive without downloading it; choose **Archive existing entries** to queue the backlist in bounded batches. Chapter grouping preserves creator-declared organization and supports persistent manual corrections.

Implemented pathways include xkcd's public JSON API, RSS/Atom and feed autodiscovery, static/declarative archives, representative WordPress/ComicFury layouts, and public WEBTOON lists/viewers. Custom layouts may require a recipe. Coverage and limitations are explicit in the [provider matrix](docs/webcomics/WEB_COMICS_PROVIDER_MATRIX.md); see [architecture](docs/webcomics/WEB_COMICS_ARCHITECTURE.md), [research](docs/webcomics/WEB_COMICS_RESEARCH.md), [metadata sources](docs/webcomics/WEB_COMICS_METADATA_SOURCES.md), [adapter guide](docs/webcomics/WEB_COMICS_ADAPTER_GUIDE.md), and [prioritized roadmap](docs/webcomics/WEB_COMICS_ROADMAP.md).

```powershell
python -m media_intake --root ./webcomic-test-data add-webcomic https://xkcd.com/
python -m media_intake --root ./webcomic-test-data list-webcomics
python -m media_intake --root ./webcomic-test-data poll-webcomics --force
```

Books search results support Ctrl/Shift multi-selection and a bulk watched-series preview. Duplicate books are reused, future books remain scheduled, and one failed series does not cancel the other additions. Applicable approved download sources now search concurrently through a shared eight-worker pool; exact title matches take precedence, then configured format/source preferences. A failed best candidate falls through to the next candidate. GetComics and other static detail connectors share public Pixeldrain, MediaFire and Dropbox resolution. Existing rights settings still apply. See the [connector validation report](docs/webcomics/DOWNLOAD_CONNECTORS_VALIDATION.md) for tested paths and live restrictions.

For reproducible tests without changing your normal library:

```powershell
python -m unittest discover -s tests -q
python tools/smoke_book_series.py
python tools/smoke_download_connectors.py
```

The book smoke downloads six public-domain Gutenberg EPUBs into its isolated `build` test library. The connector smoke performs bounded public discovery only; it does not download release files or enable sources. The Windows **Media Archive Importer (Latest)** shortcut starts `run_gui.py` from this checkout, so reopening it uses current local code. It does not fetch Git updates or reload an already running window.
