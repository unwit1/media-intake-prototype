# Downloads

Media Intake downloads files itself. The useful parts of standalone download
managers (Gopeed, Xtreme Download Manager, FileCxx) are built in, so an
external manager is optional. Downloads only: nothing is streamed or played.

## Download engine (`media_intake/transfer.py`)

- **Multiple connections per file.** A one-byte range probe checks whether the server
  supports byte ranges and how big the file is. Files over 4 MB are split across up
  to 16 connections (4 by default) into a preallocated part file.
- **Resume.** Progress is saved beside the part file (`.partial-<queue id>.part.json`)
  about once a second. A paused, failed or interrupted download continues where it
  stopped, including after the app restarts. Downloads that were running when the app
  closed go back to *queued* and resume when downloaded again. `If-Range` with the
  server's ETag/Last-Modified makes sure a changed file starts over instead of mixing
  versions.
- **Retries.** Timeouts, connection drops, 408/425/429/5xx and early disconnects retry
  with backoff, honouring `Retry-After`.
- **Pause, cancel, speed limit.** The Queue tab has *Download / resume selected*,
  *Pause*, *Cancel* (removes partial data) and *Pause all*. The speed limit is shared
  by all downloads and applies to running transfers as soon as it is saved.
- **Progress.** Speed, time left and connection count show in the queue's progress column.
- **Safety checks.** HTML pages are rejected before anything is written, free disk
  space is checked first, and expected size, SHA-256 and archive/EPUB/PDF validation
  run as before. The per-file ceiling is 256 GiB.
- **Real file type.** When a link's file type had to be guessed (a shortlink or copied
  link), the server's filename corrects it.

Settings → Downloads → *Download engine*: connections per file, speed limit (KB/s,
0 = unlimited) and retries.

**Automatic queue.** With *Start queued downloads automatically* on (off by default),
queued items start by themselves, up to *Downloads at once* (1–8, default 6). That
includes downloads interrupted when the app last closed. If one of them is a shortlink
that needs a click, the shortlink helper opens for it. The same item never downloads
twice at once, whether it was started automatically, from *Download queued* or by hand.

**Schedule.** *Start automatic downloads only from hour … until hour …* limits the
automatic queue to a daily window in local time, for example 1 until 7 for the small
hours. A window can wrap past midnight (23 until 7). Equal hours, the default, mean any
time. Running downloads finish normally, and downloads you start by hand ignore the
window.

## Source health

Every queue download, of every kind (HTTP, IRC, serial, fanfiction, torrent, stream),
and every watched-item source search records an outcome for its source. Pauses,
cancels, shortlinks waiting for the helper and approval refusals are not counted.
**Source health** on the Queue tab shows, for each source:

- its status
- recent successes
- downloads and searches that worked or failed
- typical download speed
- when it last worked
- the latest error

A source is *failing* when its last three attempts failed, or its last three downloads
failed even if its searches still work. It is *degraded* when fewer than half of its
recent attempts succeeded. Searches that find nothing are not failures. Automatic
downloads try equally good matches from a failing source last. History is capped at
200 events per source and can be cleared per source.

## Shortlinks and file hosts, automatically

When a queued link is a shortlink or a file-host share page (Pixeldrain, MediaFire,
Dropbox, Google Drive, OneDrive), the download step resolves it to the real file
first. Nothing needs to be done by hand.

- Google Drive and OneDrive work for files shared publicly ("Anyone with the
  link"). Google Drive downloads come from Drive's own download host, with the
  large-file "can't scan for viruses" notice accepted as *Download anyway* would.
  OneDrive uses Microsoft's shares API. Private files and folders are left for
  you to open.

- Shorteners are recognised from the `@include`/`@match` rules of the vendored
  [Bypass All Shortlinks Debloated](https://codeberg.org/gongchandang49/bypass-all-shortlinks-debloated)
  userscript (`media_intake/data/shortlinks`, pinned with checksums in `SOURCE.json`;
  re-pin with `tools/update-shortlink-userscript.py`). File hosts and their CDN
  subdomains are never treated as shortlinks.
- Headless resolution follows HTTP redirects, destination parameters (plain or
  base64), meta refreshes and literal script redirects, up to six hops.
- If a shortlink needs a click, a timer or a CAPTCHA, the item waits as
  *needs_shortlink*. Downloading it opens the **shortlink helper**, a WebView2 window
  that runs the userscript. You solve any CAPTCHA; the script's CAPTCHA auto-solving
  is locked off. When the page reaches a file host or file, the link comes back and
  the same queue item continues downloading.
- Download-site connectors keep such shortlinks as results (sorted after directly
  downloadable files) instead of dropping them.
- *Resolve shortlink* (Search tab, Add a direct file) does the same for a pasted link.

## Torrents (`media_intake/torrents.py`)

Magnet and `.torrent` links queue like any other item and download natively when
libtorrent is installed:

```
pip install -r requirements-torrent.txt
```

The torrent engine uses DHT, local peer discovery and UPnP/NAT-PMP. Progress shows
peers, speed and time left. Pause keeps fast-resume data
(`.partial-<queue id>.fastresume`), cancel deletes partial files, and the speed limit
applies. If there is no progress for 15 minutes, the item fails with its progress
kept, and retrying resumes it. Without libtorrent, torrent results go to an external
manager (Gopeed) instead.

**Seeding.** A finished torrent is marked *downloaded* straight away and then seeds in
the background. It stops at whichever comes first: a share ratio (default 100%,
meaning you upload as much as you downloaded) or a time limit (default 24 hours).
Both, plus an upload speed limit, are under Settings → Downloads. Setting the ratio
and the time limit both to 0 turns seeding off. The queue's progress column shows the
ratio, peers, upload speed and time left. *Stop seeding* on the Queue tab ends it early
and keeps the files. Seeding state (`.seed-<queue id>.fastresume`) survives restarts,
so unfinished seeding continues when the app opens again.

## Video streams saved to a file (`media_intake/streams.py`)

HLS streams are saved as an ordinary file. Nothing is played. A queued `.m3u8` link,
or any link whose server answers with an HLS playlist, is handled automatically:

- The best variant of a master playlist is chosen.
- Segments download in parallel, using the same connection, retry and speed-limit
  settings as other downloads.
- Finished segments are kept in `.partial-<queue id>.hls/`, so pause, failures and
  restarts resume. Cancel removes them.
- Byte-range segments and fMP4 initialisation sections are supported.
- Live playlists are recorded until the stream ends. *Pause* stops a live recording
  and keeps what was recorded.
- MPEG-TS segments are joined into `.ts`, and fMP4 into `.mp4`. When
  [ffmpeg](https://ffmpeg.org/) is on PATH, `.ts` is remuxed to `.mp4` without
  re-encoding, and streams that keep audio in a separate track are merged (those
  require ffmpeg).
- **Encrypted streams are refused, not decrypted.** That means any `EXT-X-KEY` /
  `EXT-X-SESSION-KEY` method other than `NONE`, including DRM key systems.

Clipboard capture recognises copied `.m3u8` links as video streams.

## IRC search and the IRC channel window

- **Configured networks.** IRCHighway `#ebooks` (`irc.irchighway.net`, TLS 6697; its
  certificate does not verify, so verification is off for it) and Undernet `#bookz`
  (`irc.undernet.org`, plain 6667, because Undernet has no TLS) are set up once for
  Books and Light novels.
  - A source you remove is not added back.
  - An existing entry for either network gets the settings it needs.
  - Other servers can be added under Settings → IRC sources.
- **Connection fixes.** A nickname already in use is retried with a numbered suffix.
  When the search bot says nothing matched, the search ends straight away instead of
  waiting for the timeout.
- **Right-click a queue item** for these options:
  - *Search all IRC servers for this book* searches every configured server at once
    and opens a results picker. It shows title, author, format, size, bot and server,
    with a filter. Add the lines you want to the queue, or add them and start
    downloading.
  - *Open IRC channel to search myself…* opens a channel window with the server
    chosen and `@search <title author>` prefilled. Type any command, including `!Bot
    file` requests. Search-result lists sent back (`.zip` or `.txt`) open in the same
    picker. A file a bot offers is saved to the library folder after you confirm, and
    added to the Library.

## Podcasts

The **Podcasts** tab follows shows and downloads their episodes automatically.

- **Finding shows:** search Apple's public podcast directory, or paste a feed URL.
- **Following:** choose how much of the back catalogue to download: none, the
  latest 3, the latest 10, or every episode. Everything else is recorded as
  *skipped*, and you can still download any episode by hand.
- **New episodes:** each show is checked on its own schedule (every 2 hours by
  default) with conditional requests. New episodes queue for the normal download
  engine, which handles resume, retries, the speed limit and the automatic queue.
  Episode identity is the RSS GUID, so a re-published feed never downloads an
  episode twice.
- **Where files go:** `Podcasts/<Show>/` in the library folder, named
  `<date> - <episode title> - <show>`.
- **Per show:** switch auto-download on or off, change the check interval, and
  optionally keep only the latest N downloaded files. Keep-latest deletes older
  episode files, so it is off by default.
- **Unfollowing** keeps the downloaded files.
- **OPML:** *Import OPML…* follows every feed in a subscription file exported from
  another podcast app, folders included. Already-followed shows are skipped, and no
  back catalogue is queued, so only new episodes download. *Export OPML…* writes your
  followed shows for any other app.
- **Approval:** the `podcast_rss` source starts approved in `rights-evidence.json`,
  because podcast feeds publish their episodes for download. Set it to false to stop
  podcast downloads.

## Email to a chosen Kindle

Right-click a book in the Library → *Email to Kindle* lists every Kindle address from
Settings → Kindle, plus *All Kindles*. Sending this way works even when auto-send is off.

## Jackett

Search tab → *Jackett (torrents)* searches the indexers configured in your own
Jackett (Settings → Downloads: address, API key, optional indexer). Results show size
and seeders and queue as torrents. They need `jackett.approved_for_download = true`
in `rights-evidence.json`, like every other source's approval switch.

## Clipboard capture

Settings → Downloads → *Watch the clipboard for download links* (off by default).
When you copy text containing magnets, `.torrent` links, file-host links, shortlinks
or direct media-file links, a small prompt offers *Queue and download*, *Queue* or
*Ignore*. Links already in the queue and ordinary web pages are ignored.

## Optional external managers

*Send to download manager* (search results and the Queue tab) still hands a link to
Gopeed (REST API, also handles torrents), Xtreme Download Manager (`--url`) or a
custom command. This is optional now that the built-in engine covers the same ground.
