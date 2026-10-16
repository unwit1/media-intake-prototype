# Download connector validation — 2026-09-29

This report distinguishes connector wiring, controlled transfers, public discovery, and real file downloads. A passing fixture does not prove that every live host/file is accessible.

## Concurrent search and selection

`watched._search_candidates` resolves applicable sources and configured priorities on the caller thread, then searches each source in a shared eight-worker pool. Workers do not use the caller's SQLite connection. Progress callbacks return to the caller thread. Results are collected deterministically, duplicate URLs/formats are removed, and exact normalized titles rank before fuzzy matches; format and source preferences break ties. Existing creator matching remains a gate. The book download loop tries the next candidate after a failed transfer.

Tests cover simultaneous sources, a faster inferior match, failures, duplicate links, denied sources, callback thread ownership, and 20 concurrent book requests sharing the same global limit. The existing book watch pool allows four active books and eight outstanding book tasks. Category search already had a bounded metadata/source pool; it now benefits from concurrent watched-source search internally too.

GetComics comic issue watches retry alternative matching hosts. Series watches persist successful release identities, so different links for the same GetComics post are treated as alternatives after one succeeds. This reconciliation is deliberately scoped to one declared release; uncertain cross-site matches are not merged. Multipart/weekly-pack semantics need a separate collection model before broad automatic support.

## GetComics and public file hosts

The public search returned three post references. Two release pages were inspected for links. Current pages wrap MEGA/Pixeldrain/main-server URLs in `/dls/` redirects, which required an additional dispatch step after the redirect. The new redirect probe stops at recognized share hosts and lets the registered host adapter handle the public metadata rather than treating a viewer HTML page as a media file.

The live Pixeldrain route resolved a CBR filename, size and SHA-256 metadata successfully. The sampled direct server returned HTTP 403 at `fs2.comicfiles.ru`; that restriction was retained. No comic release body was downloaded. Searching for the text "public domain" also returns a commercial comic with that title; the query is **not** evidence of a public-domain license.

| Path | Implemented behavior | Validation and limits |
|---|---|---|
| GetComics search | Official HTTPS host, bounded five-page search, duplicate/end-of-pagination guards | Live public search; synthetic 100 repetitions of a 1,000-link page; max 50 unique results |
| Main server/direct files | Bounded polite HEAD redirects; content/disposition extension detection | Real local CBZ transfer and failed-host fallback; sampled live host returned 403 |
| Pixeldrain | Official public file-info API, filename/type/size/hash, availability restrictions; single files | Live wrapped link metadata resolved; fixture → actual local HTTP → byte/checksum validation |
| MediaFire | Public `downloadButton`, validated download subdomain and file type | Fixture → actual local HTTP transfer; rejects ad URLs and missing/challenge buttons; no supplied live test file |
| Dropbox | Official shared-file `dl=1` transformation preserving `rlkey` | Fixture → actual local HTTP transfer; no supplied live test file |
| MEGA | Explicit manual/session-required result | Not implemented as a decrypting/downloading client; legitimate client/session connector is backlog |
| WeTransfer | Explicit manual/session-required result | Expiring transfer/session connector is backlog |
| Terabox / 1024terabox | Explicit manual/session-required result | Live link inventory confirms these variants; no authentication automation |
| GoFile / Google Drive / 1fichier | Explicit manual/session-required result | Proper credentials/session/wait handling needed; no fabricated direct URLs |

Host restrictions, CAPTCHAs, quotas, takedowns and missing files stop extraction and produce warnings. No limit bypass, alternate proxy, CAPTCHA solver or private-session borrowing is implemented. The host registry also serves static detail connectors in `download_connectors.py`, retaining the originating source's provenance.

Core HTTP downloads now verify Content-Length when available, host-declared size and SHA-256 before completing the queue item. Failures remove partial files and retain an actionable failed state. Each public file-host path was tested through real local HTTP with original bytes; a deliberately wrong checksum failed safely.

## Current source coverage

`tools/smoke_download_connectors.py` checked 24 source keys using at most one bounded public query each (four parallel domains). It does not download release files. Dated evidence is retained in [research/download-connector-probes.json](research/download-connector-probes.json). A zero-result query cannot distinguish no matching work from parser breakage; these sources are marked unverified rather than healthy.

| Source(s) | Controlled coverage | Observed live discovery |
|---|---|---|
| GetComics | Search/pagination, post/direct/wrapped hosts, download, issue matching, mirror retry/deduplication, missing/denied/challenge paths, parser load | Search and post metadata work; wrapped Pixeldrain metadata works; sampled main file server 403 |
| LibGen | Existing row/detail parsers and watched download routing tests | 14 metadata results; no files fetched; title filtering rejects unrelated editions |
| Anna's Archive | Existing search/detail tests | HTTP 403; no circumvention |
| LNORI / NovelArchive | Existing catalog/resolution/chapter tests, local EPUB downloads | Metadata references returned; broad query can return unrelated titles, so match gating remains necessary |
| Internet Archive | Public metadata/files, restricted-file omission, redirect checks, local EPUB transfer | No eligible file from one bounded sample; lending/restricted items excluded |
| GitHub FOSS | Identified license/release-asset parser, local ZIP transfer | No eligible asset from one-repository sample; not proof of general failure |
| SourceForge | Project/release parser and allowed package tests | HTTP 403; live transfer unverified |
| FOSShub | Explicit open-source metadata catalog tests | No matching sample; metadata-only connector, not binary downloader |
| Meawfy / SoftArchive | Detail parser plus actual local EPUB transfers and shared host resolution | No matching sample; live downloads unverified |
| Anime Tosho / Tokyo Insider / AnimeOut / NoobSubs / Chauthanh | Each adapter search/resolution; each static connector's category-compatible local transfer | Four zero-result samples; NoobSubs HTTP 522 |
| MediaFireTrend / WarezOmen | Registered search/direct parser; local category file transfer | MediaFireTrend browser fallback; WarezOmen zero-result sample |
| MyAbandonware / OldGamesDownload / AbandonwareGames / GOG Revived / AnkerGames | POST/search/registered parser tests; static detail resolution and local ZIP transfer | MyAbandonware/OldGamesDownload zero results; AbandonwareGames/GOG browser fallback; AnkerGames 404 |
| XDCC.eu / configured IRC | Directory parsing, format/category/priority tests and existing controlled transport/concurrency tests | XDCC directory zero results; no live IRC bot commands or credentials used |
| OPDS / JSON catalogs | Existing parser/download tests; catalogue and queue fixture tests | User-specific endpoints not probed |
| MangaDex / Taddy / 3S / RSS comics | Existing API/feed/serial tests plus new normalized Webcomics tests | xkcd live archive resolved separately; credentialed services not probed |
| FanFicFare / story connectors | Existing adapter/queue/update tests | No new live story downloads; dependency/session availability remains environment-specific |
| Gutenberg direct files | Three watched series, six EPUBs, duplicate batch check | **Six actual public-domain book downloads passed**, isolated under `build/book-series-live-smoke` |

## Follow-up with acceptance criteria

| Priority | Dependency / affected files | Recommended work | Acceptance |
|---|---|---|---|
| P1 connector health | Existing fixture/report tooling; `download_connectors.py`, GUI | Persist source success/failure/latency, expected parser signatures, diagnostics and browser/source links | Empty layouts distinguished from legitimate no results; one failed provider never hides other results |
| P1 ranking evidence | Current source pool and book identifiers | ISBN/work/edition/language matching, explain selected candidate, bounded top-candidate availability probes | Wrong book/issue/language never auto-selected; preference and match evidence visible; no full file downloads merely to rank |
| P1 transfer reliability | Core validation and host registry | Expiring URL re-resolution, host limits on streaming transfers, Retry-After scheduling, cancel/retry and fallback queue history | Expired MediaFire link renews once; 429 defers host; cancelled transfer leaves no complete file or automatic delivery |
| P1 connector fixtures | Live report findings | Add legally practical real-layout fixtures for zero-result sources; repair current Anker route after fresh official/public research | Each repaired source has positive/empty/challenge fixtures and a fresh public discovery check |
| P2 legitimate session clients | Secret store/session interface; `file_hosts.py`, core queue | MEGA via a licensed official client; WeTransfer/GoFile/Drive/Terabox only through permitted sessions/APIs | Credentials kept out of manifests/logs; expiry and quotas surfaced; public fixture plus user-owned live test file downloads |
| P2 collections | Release and archive model | Pixeldrain lists, folders, multipart archives/weekly packs with per-file selection and completeness | Mirror fallback does not skip a distinct part; one completed release is not archived again from another mirror |

References: [Pixeldrain API](https://pixeldrain.com/api) documents public file metadata, checksums, CAPTCHA/quota/takedown errors; [Dropbox shared-file parameters](https://help.dropbox.com/share/force-download) documents `dl=1` and `rlkey`; [GetComics download guide](https://getcomics.org/how-to-download/) describes main/external services; [Kapowarr upstream](https://github.com/Casvt/Kapowarr) and its [download-client documentation](https://kapowarr.barrylee.technology/settings/downloadclients) provide architectural references. No Kapowarr or other upstream implementation code was copied.
