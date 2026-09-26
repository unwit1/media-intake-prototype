# Media Intake prototype

A local Python 3.10+ worker for finding, queuing, downloading, renaming, and importing ebook files. It has no third-party Python dependencies. Queue and provenance are stored in SQLite under `--root`; downloaded files are checked, hashed, and named from title, author, and optional series. It can import one completed file into Calibre with `calibredb add`. There is no account automation, background daemon, or copyright classification in this prototype.

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

The GUI provides source search, result review, direct URL intake, queue status, downloads, and a persistent **Watched** tab. Network work runs in the background so the window remains responsive. It uses the same SQLite queue and download validation as the command line commands.

### Watched books, series, and authors

In the **Watched** tab, paste a public Goodreads book, series, or author URL. The app reads the page's public title, author, and release date when available; you can also enter those fields manually. Pick a saved library folder and choose which supported sources to search. A book with a future release date is marked **Unreleased** and is first searched on that date. If no matching file is found, it retries daily. Matching downloads are saved into the chosen folder. The date checks and retries run while the GUI is open, and overdue checks resume the next time it starts.

For a Goodreads series or author, the app remembers the books already listed and checks the page daily for new entries. It watches newly listed books, plus any upcoming books whose release dates Goodreads exposes, without queueing the existing back catalog. Goodreads may restrict or change public page markup, so a URL that cannot be read can still be added by entering its title and author; discovery requires a readable Goodreads page.

On Windows, use `py` in place of `python` if needed. Put `--root "C:\\Media Intake"` before the subcommand to choose where data lives. `search-openlibrary` uses Open Library's search API and Internet Archive's item metadata to find actual EPUB/PDF links; results are not added to the queue until selected. The live search and download were tested with *Alice's Adventures in Wonderland* from Internet Archive. Some item records will lack accessible ebook files.

## Website sources

The prototype supports these discovery paths:

| Source | Command | Status |
| --- | --- | --- |
| Open Library / Internet Archive | `search-openlibrary QUERY` | Live search and EPUB download tested |
| OPDS catalogs, including Standard Ebooks when its feed is reachable | `search QUERY --opds FEED_URL` | Parser tested locally |
| Local JSON catalogs / Project Gutenberg direct links | `search QUERY --catalog examples/catalog.json`, or `add --url ...` | Direct downloads tested with three Gutenberg EPUBs |
| Anna's Archive | `search-site annas QUERY --base SITE_URL` | HTML adapter fixture tested; live site returned HTTP 403 in this workspace |
| Library Genesis | `search-site libgen QUERY --base SITE_URL` | Search, detail link resolution, and EPUB download tested on `libgen.gl`; other domains may differ |

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

Edit `examples/irc-server.json` to specify your server, channel, nickname, bot, numeric pack, and book metadata. Then:

```bash
python -m media_intake add-irc examples/irc-server.json
python -m media_intake list
python -m media_intake download 1
```

The client connects to that server (TLS by default), joins the channel, sends the commands, waits for `DCC SEND` **from the configured bot**, and receives one file. The DCC file name is never used as a local path; the received extension must match `format`. The normal queue, hash, archive validation, retry, and Calibre import steps apply. Set `password_env` to the *name* of an environment variable if the IRC server needs a connection password; never put the password in the JSON file.

`commands` accepts 1–10 entries using `/msg TARGET TEXT`, `/notice TARGET TEXT`, and `/join CHANNEL`. `{bot}`, `{pack}`, `{request}`, and `{channel}` are replaced from that server's config, so different servers can use commands such as `/msg {bot} xdcc send #{pack}` or `/msg {channel} !get {pack}`. The default for numeric packs is the XDCC example. It supports ordinary active DCC SEND; reverse/passive DCC, resuming partial transfers, SASL, and CAP negotiation are not implemented. The IRC server can use TLS, but ordinary DCC file transfer is a direct unencrypted connection and exposes the receiver's IP address to the sending peer.

## Comics, manga, webtoons, web fiction, and fanfiction

For a direct EPUB, PDF, or CBZ file, use `add --url` as above. For chapter pages, use an ordered manifest. Supported `kind` values are `comic`, `manga`, `webcomic`, `webtoon`, `webfiction`, and `fanfiction`. The first four fetch chapter images and build a numbered CBZ; the last two extract text paragraphs and build an EPUB with a table of contents. Files then use the same queue, naming, hashing, and Calibre import commands.

```bash
cp examples/serial.json my-serial.json
# Edit title, author, kind, selector, and ordered chapter URLs.
python -m media_intake add-serial my-serial.json
python -m media_intake download 1
python -m media_intake import-calibre 1 --library "/path/to/Calibre Library"
```

For sites publishing RSS or Atom chapter feeds, discover a draft manifest, review its chapter order and selector, then queue it:

```bash
python -m media_intake discover-feed "https://example.org/story/feed" --kind fanfiction > my-serial.json
python -m media_intake add-serial my-serial.json
```

The `selector` accepts comma-separated HTML tags, `.classes`, and `#ids`, such as `article,.chapter-content,#reader`. Text extraction reads paragraphs and headings within the selected container; image extraction uses `data-src`, `data-lazy-src`, `data-original`, `data-url`, `src`, and `srcset`. Configure chapter URLs explicitly for sites without a feed. Browser-rendered chapters, account-only pages, scripts that populate content, complex pagination, and unusual image loaders need site-specific adapters. The example URLs are placeholders. Feed discovery reads the latest feed page and reverses its typical newest-first order; review it when a feed uses a different ordering. The prototype does not automatically crawl all chapters on an arbitrary site.

## Queue behavior and development

`download` is explicit; search results never fetch themselves. Repeating `add` for the same URL preserves the queue entry. Repeating `download` reuses an existing completed file. If a named file already exists with different bytes, a short SHA-256 suffix is added. A failed download records its error and can be retried with `download ID`. Invalid EPUB/CBZ archives and PDF headers are rejected without leaving partial files. Files land under `<root>/downloads/`.

`python -m unittest discover -s tests -v` runs local HTTP, source-parser, Calibre invocation, and fake IRC/DCC integration tests. These tests do not contact live ebook/IRC servers. Calibre must be installed and `calibredb` on PATH for a real import; this workspace lacked it.

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

Every push to `main` builds a single-file `MediaIntake.exe` on a Windows runner, uploads it as a workflow artifact, and updates the private `portable-latest` GitHub release. Download the executable directly from [the latest release](https://github.com/unwit1/media-intake-prototype/releases/latest/download/MediaIntake.exe); no Actions-page navigation is needed. Python and Git are not needed to run the executable. The executable stores its queue under `%USERPROFILE%\MediaIntakeData` by default.

To build it locally on Windows instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build-portable.ps1 -Clean
.\dist\MediaIntake.exe
```

The build script uses PyInstaller only for packaging. The application itself has no third-party runtime dependencies.
