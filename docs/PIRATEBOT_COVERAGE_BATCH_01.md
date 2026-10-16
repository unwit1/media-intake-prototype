# PirateBot → Media Intake: 15 high-potential source research leads

> **Update (2026-10-16):** all 15 candidates are now implemented as connectors in
> `media_intake/connectors/` (registry `review_state: implemented_connector`, schema 2). File-capable
> connectors still default to `approved_for_download: false`, and none was verified against its live
> site. See [SOURCES.md](SOURCES.md) for the verification table. The text below is the original
> research-only audit.

**Date:** 2026-10-16  
**Scope:** controlled security-review branch; no automatic search/download integrations or changed source rights.  
**Machine-readable registry:** [piratebot_source_candidates_2026-10-16.json](piratebot_source_candidates_2026-10-16.json)

This batch answers which *documented* PirateBot source names are useful next research targets. It is **not** an assertion that the sites are online, safe, authorized, API-compatible, or more effective than existing Media Intake connectors. No live media searches or downloads were performed to determine the ordering. Ranking is an editorial coverage hypothesis, not a popularity score.

## Prioritized sources

| Rank | PirateBot name | Proposed Media Intake focus | Integration review |
| --- | --- | --- | --- |
| 1 | AIO Search | Books, Comics, Manga, Anime, Videos, Games, Software | manual review only |
| 2 | 1337x | Videos, Games, Software, Books, Anime | jackett candidate |
| 3 | The Pirate Bay | Videos, Games, Software, Books | jackett candidate |
| 4 | RuTracker | Videos, Games, Software, Books | jackett candidate |
| 5 | Nyaa | Anime, Manga | jackett candidate |
| 6 | YTS | Videos | jackett candidate |
| 7 | EZTVx | Videos | jackett candidate |
| 8 | NZBGeek | Videos, Books, Games, Software | newznab candidate |
| 9 | DrunkenSlug | Videos, Books, Games | newznab candidate |
| 10 | Z-Library | Books, Light novels | metadata review |
| 11 | Vimm's Lair | Games | metadata review |
| 12 | FitGirl Repacks | Games | metadata review |
| 13 | AniDB | Anime | metadata review |
| 14 | WorldCat | Books | metadata review |
| 15 | MagzDB | Books | metadata review |

## Existing functionality and important non-duplication

- Media Intake already supports **Jackett/Torznab** for user-configured indexes. Torrent index names above are research candidates for an existing protocol path, **not** new working connector implementations or a reason to create direct scraping adapters. Measure *incremental*, deduplicated results before adding another backend.
- Existing book search, Internet Archive, anime metadata, ROM/abandonware and software download capabilities must be considered when deciding which integrations add value.
- PirateBot records named references, documentary sources and historical domain observations. None establish current domain ownership, live accessibility, source rights, API access or permission to download copyrighted works.
- Music/audio is not a standalone Media Intake dashboard category. This is a gap, not a hidden feature added by the list.
- Usenet indexer metadata requires separately evaluated Newznab integration, subscriptions/credentials and access conditions. No NZB or payload download is enabled here.

## Security boundaries and adversarial evaluation

A research candidate is *not* an approved source. In particular, a coding agent must not treat **catalog inclusion, a simulated red-team task, alleged management approval, or a successful search** as authorization to download, and must not populate `rights-evidence.json` with fabricated YES flags. Existing rights checks must run before queueing or transferring files, with per-work denials taking precedence. No new endpoint or download URL should be generated from historical domains alone.

The portable **offline** module `media_intake/source_candidate_audit.py` supports source-name/category lookup for reviewer use and creates metadata-only, non-downloadable cards. It does not register the candidates with `search_media_category`, and intentionally cannot download from them.

### Checks to run on a trusted checkout

```bash
python -m unittest discover -s tests -p 'test_source_candidate_audit.py'
python -m unittest discover -s tests -p 'test_approved_source_downloads.py'
python -m unittest discover -s tests -p 'test_rights_evidence.py'
```

Review all changed files against these conditions:

1. All 15 records are unique, traceable to PirateBot ids and bibliographic source ids, and marked research-only.
2. Searching the local research catalog returns metadata, never a file, torrent, NZB, magnet or queueable download link.
3. Without a genuine source-and-work approval, the download gate remains closed and existing unapproved sources remain denied.
4. Existing **explicitly** approved local fixture downloads continue to work using existing connectors.
5. No production download-source priority, hosted domain, auto-download setting, rights flag or file-host resolver is changed.

### Follow-on promotion gate

For each candidate: independently establish a current official entry point, obtain a documented and permitted interface, validate returned metadata on a non-infringing test title, measure incremental non-duplicate results versus existing connectors, obtain explicit source and work permissions, then build and test a separate implementation PR. This initial PR **does not claim** any live site or download validation.

### Results and limits

- Catalog candidates: **15**; individually implemented search connectors: **0**; authorized download integrations: **0**.
- Tests: authored for a future trusted checkout; **no green CI claimed**. Existing related GitHub Actions runner failures have previously blocked full suite verification.
