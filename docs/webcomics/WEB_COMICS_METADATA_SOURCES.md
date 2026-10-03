# Metadata providers, matching and attribution

Assessed 2026-09-29. Content locations and metadata catalogs are separate relationships. A metadata search result is not a download entitlement or an automatic identity match.

| Provider | Practical webcomic coverage / identifiers | Access, provenance and integration decision |
|---|---|---|
| Creator/publisher website | Strongest authority for creator-defined chapters, credits, canonical URLs and copyright | Implemented HTML/structured extraction. Store source URL, selector/field evidence and adapter version; manual locks outrank later inference |
| [Schema.org ComicSeries](https://schema.org/ComicSeries) | Series/creative-work schema, author/artist, language, image and copyright | Implemented JSON-LD metadata provider for ComicSeries/CreativeWorkSeries/BookSeries and graphs. Extend to linked `isPartOf`, ItemList/ComicIssue and typed dates with fixtures |
| [OpenGraph](https://ogp.me/) | Site name, description, cover and locale; weak organization evidence | Implemented lower-confidence field candidates. OG images remain covers, not comic content. Website names can differ from series names |
| Native platform/feed metadata | Best source for episode IDs, publication differences and image order | Implemented feed and xkcd candidates; reuse existing Taddy/3S semantics. Platform-native creators/ratings/tags expand with adapters |
| [Wikidata comics project](https://www.wikidata.org/wiki/Wikidata:WikiProject_Comics) | Creator/site/QID relationships; some established webcomics, sparse chapters | Existing `wikidata.py`/media graph reusable. Wikidata structured data is [CC0](https://www.wikidata.org/wiki/Wikidata:Licensing). P1 bridge should favor exact canonical homepage/QID matches over labels |
| Wikipedia | Established comic descriptions, citations, creator histories | Use official [MediaWiki API](https://www.mediawiki.org/wiki/API:Main_page), retain article/revision and attribution; text has its own license. P2 enrichment, never use encyclopedic prose as a content archive |
| Comic Vine | Printed issues/volumes and creators, uneven creator-webcomic coverage | Existing credentialed `comics.py` lookup and issue tracking. Review [API terms](https://comicvine.gamespot.com/api/) before new use; catalog IDs, publication/store dates and credits remain distinct |
| MangaUpdates | Manga/manhwa/webtoon catalog aliases, licensing/language publishers, releases | Existing `media_catalogs.py` search. [API entry point](https://api.mangaupdates.com/) requires current endpoint/rate documentation review. Metadata matching only; catalog releases may be translations or groups, not original creator releases |
| [AniList API](https://docs.anilist.co/) | Some webtoon/manhwa/print mappings; AniList ID, titles, staff roles, start/end dates | Existing search/fetch in `comics.py`, graph relationships. Reuse wrappers with terms/rates; no assumption every western webcomic is represented |
| [MyAnimeList API](https://myanimelist.net/apiconfig/references/api/v2) | Manga/print mappings; MAL IDs | Application registration/OAuth. P2 optional provider, crosswalk via explicit IDs rather than title-only automatic merge |
| [Kitsu](https://kitsu.docs.apiary.io/) | Manga/anime-oriented titles and mappings | P2 optional metadata connector; first verify current API availability/terms. Catalog kind may differ from source format |
| Goodreads | Printed collected editions, book/series/author relationships | Existing public HTML/manual import path; no assumption of a broadly available public API. Edition ISBN is evidence of an edition, not proof of identical web episodes |
| The StoryGraph | Reading/catalog information for printed editions | P3 user-assisted mapping/import; no established public API integration verified in this pass. Do not scrape private reading data |
| [Open Library APIs](https://openlibrary.org/developers/api) | Collected books, works, editions, authors/ISBNs; uneven webcomics | Existing book metadata/search and series code. Work/edition IDs and ISBNs enrich print relationships; not page or chapter numbering authority |
| [Grand Comics Database](https://www.comics.org/) | Printed comic issues, credits and indexing; weak web-native episode coverage | Implemented public issue/series search in Add Anything Comics; documented unstable API contract, bounded overview/number lookups, attribution and CC-BY-SA-4.0 catalog provenance. Live request timed out; synthetic contract tests pass. See [issue-search report](COMICS_METADATA_SEARCH.md) |
| [Metron](https://metron.cloud/) | Printed issues/series and occasional cross-IDs to GCD/Comic Vine | Implemented optional token-authenticated Comics search, exact numbering/run matching and catalog type classification; 3.1-second spacing and bounded pages. Credentialed live verification and richer issue details remain backlog |
| [ComicInfo.xml](https://anansi-project.github.io/docs/comicinfo/documentation) | Local series/title/number/credits/Web/StoryArc/page fields | Existing exporter reused. New CBZ contains ComicInfo and richer JSON manifest. Import/round-trip and XSD validation are P1/P2 backlog; XML export alone cannot represent all provenance |
| [OPDS 1/2](https://specs.opds.io/) | Acquisition catalogs and publication metadata, not universal series discovery | Existing Atom OPDS book search. OPDS2/reader streaming export is P3. Keep catalog acquisition URLs separate from creator episode URLs |
| [Taddy](https://taddy.org/developers/intro-to-taddy-graphql-api) / 3S | Catalog of participating comics and public ComicSeries issue feeds | Existing credentialed metadata search and public feed parsing remain intact. Provider coverage is opt-in; exclusive/availability dates must remain respected |

## Implemented metadata interface

Providers register with `MetadataRegistry`. An extractor emits `MetadataCandidate(field, value, provider, source_url, confidence, evidence)`. `merge_metadata` keeps candidate values separately from selected values and honors explicit field locks. Feed and API adapters use that same representation; metadata fetching is not coupled to image downloading. Provider IDs, source URLs and evidence are visible in the detail tab and archived manifests.

External-service `search/lookup` wrappers and persistent identity-review UI are prioritized next. Reuse existing `comics.py`, `media_catalogs.py`, `wikidata.py`, `media_resolver.py` and `media_graph.py`; do not create parallel Wikidata/AniList/Comic Vine clients.

## Matching rules

1. Exact canonical content URL or a verified provider+external ID can establish identity.
2. Normalized title/alternate title plus creator overlap produces a review candidate; `match_entities` never automatically merges this evidence.
3. ISBN and publisher/publication dates identify printed editions, which may be related to a webcomic without being the same edition.
4. Language, source platform and publication/episode numbering differences must survive a confirmed source attachment.
5. A user-selected identity or organization is persisted as an override/lock. Later heuristics must not silently reverse it.

Store provenance per individual field and source, including lookup/revision time as external connectors are added. Never concatenate conflicting biographies, silently overwrite a creator's chapter tree, or convert metadata-source image thumbnails into story pages.
