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

## Shortlinks and file hosts, automatically

When a queued link is a shortlink or a file-host share page (Pixeldrain, MediaFire,
Dropbox), the download step resolves it to the real file first. Nothing needs
to be done by hand.

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
applies. Transfers stop when complete, with no seeding. If there is no progress for
15 minutes, the item fails with its progress kept, and retrying resumes it. Without
libtorrent, torrent results go to an external manager (Gopeed) instead.

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
