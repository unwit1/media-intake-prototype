# Media Intake Roadmap

This roadmap extends the existing Media Intake watched-media system from title/series-level monitoring into a general **franchise, continuity, and release graph** that can automatically discover related media with minimal manual input.

The implementation order is intentional:

1. **Franchise / continuity auto-expansion**
2. **Game watching**
3. **Video watching**
   - Movies
   - TV / episodic series
   - Web series
   - Shorts, specials, OVAs/OADs, miniseries, streaming originals, and other video-first formats

Later media types should reuse the same entity, relationship, provenance, refresh, confidence, and review infrastructure rather than creating independent one-off watch systems.

---

## Guiding principles

### Minimal-input intake

The normal user flow should be:

1. Enter or select one title.
2. Media Intake resolves the entity and media type.
3. The app discovers its series/franchise/continuity relationships.
4. High-confidence direct continuations and installments are discovered and linked; individual watch rows are created only according to the user's media watch policy.
5. Related but weaker connections are retained as discovered candidates without blocking the import.
6. Future refreshes discover newly announced installments, sequels, spin-offs, release-date changes, and other relevant changes automatically.

Manual curation should be the exception.

### Discovery is broader than auto-inclusion

The resolver should aggressively discover related entities but classify them before deciding what becomes watched.

Default policy target:

- Auto-add: direct numbered installments, direct sequel series, direct prequel series, canonical in-series novellas/side works, announced future installments.
- Discover and surface: spin-offs, character continuations, interquels, crossovers, shared-universe branches.
- Track separately: adaptations, remakes, reboots, alternate continuities, editions.
- Ignore by default: unrelated works that merely share a creator.

When the user selects a series from search, keep that series as one Watched item.
Record its books and related works in the graph; do not turn the series lookup into
a list of per-book watches. The series title is searched as a series through the
connected sources.

### Provenance over silent guesses

Every normalized relationship should preserve:

- provider/source
- provider entity ID
- raw relationship type
- source URL when available
- observed timestamp
- confidence
- whether the relationship is inferred or explicitly supplied by the source

Conflicting metadata should remain auditable rather than being silently discarded.

---

# Phase 1 — Franchise and continuity auto-expansion

**Priority: P0**

This is the foundation for every later roadmap phase.

## Goal

Allow a user to add a single book, series, game, film, show, or web series and have Media Intake automatically discover the larger structure around it.

Examples:

- Add **Percy Jackson & the Olympians** → discover **Heroes of Olympus**, **Trials of Apollo**, relevant spin-offs, crossovers, and future related series.
- Add **The Stormlight Archive** → resolve its main books, novellas/short works, future installments, editions, collections, and larger continuity relationships without treating every publication as a unique story.
- Later, the same graph can represent a game franchise, film franchise, television universe, or mixed-media property.

## New core entities

Add provider-independent entity types:

- franchise
- universe
- continuity
- series
- season
- arc
- work
- edition
- collection
- release

Not every property needs every level.

## New relationship model

Create a normalized relationship vocabulary including at minimum:

- contains
- part_of
- parent_series
- subseries
- follows
- followed_by
- sequel_to
- prequel_to
- direct_sequel_series
- direct_prequel_series
- spin_off_of
- character_spin_off
- interquel
- parallel_story
- crossover_with
- same_universe
- adaptation_of
- remake_of
- reboot_of
- alternate_continuity_of
- edition_of
- collection_of
- includes_work
- supersedes

Allow multiple simultaneous relationships between the same entities when supported by evidence.

## Work vs edition/container separation

Do not treat packaging as a new story.

Required behavior:

- hardcover/paperback/ebook/audiobook → editions of one work
- split-volume editions → parts/editions of one work
- omnibus/box set → collection/container
- anthology → collection that contains works
- revised/deluxe release → edition/release unless evidence indicates a new work
- alternate draft → related work, not a numbered installment

The watched object should normally be the logical work, not every edition.

## Provider federation

Implement a provider-neutral resolver.

### Universal relationship/disambiguation layer

- Wikidata
- first-party creator/publisher/studio sources when available

### Books

Reuse and extend existing integrations:

- Goodreads
- Open Library
- RanobeDB
- AniList where relevant

Add candidate structured providers where practical:

- Hardcover
- Google Books series metadata
- ISFDB
- BookBrainz

No single provider should be canonical for every field.

## Resolver API

Introduce internal interfaces similar to:

- media.resolve(query_or_url)
- media.series.expand(entity_id)
- media.franchise.discover(entity_id)
- media.franchise.expand(entity_id)
- media.continuity.discover(entity_id)
- media.relationships.resolve(entity_id)
- media.graph.refresh(entity_id)
- media.graph.diff(entity_id)

The UI and CLI should call provider-independent services rather than individual source adapters directly.

## Relationship confidence

Store a relationship confidence score and evidence set.

Inputs may include:

- source authority
- explicit vs inferred relationship
- source recency
- agreement across sources
- identifier agreement
- title/creator/year matching
- first-party confirmation

Low-confidence relationships should enter review without blocking high-confidence imports.

## Watch policy

Add inheritable watch-policy settings.

Suggested hierarchy:

1. global defaults
2. media-type defaults
3. franchise override
4. series override
5. individual work override

Suggested policy keys:

- future installments
- direct sequels
- direct prequels
- canonical novellas/side works
- spin-offs
- crossovers
- shared-universe branches
- adaptations
- remakes/reboots
- alternate editions

Each can support behavior such as:

- auto_add
- discover
- ignore
- metadata_only

## Refresh and diff

A watched franchise/series should periodically refresh its graph and compare the latest provider state against the stored graph.

Detect at minimum:

- new installment
- new sequel/prequel series
- newly titled placeholder
- release-date added
- release-date changed/postponed
- status changed
- relationship clarified
- new edition
- new collection
- new adaptation
- series completion/cancellation when available

Do not recreate existing entities when metadata changes.

## GUI

Extend the existing **Watched** tab with:

### Add Anything

One input that can accept:

- title
- series/franchise name
- URL
- provider ID where supported

### Import preview

Before first save, summarize:

- resolved entity
- media type
- direct installments found
- future installments found
- related continuities found
- editions/collections collapsed
- unresolved ambiguities

Normal users should not need to select providers manually.

### Related / Continuity view

Show the relationship graph as a readable hierarchy/list first. Graph visualization can come later.

### Needs Review

Create a non-blocking review queue for:

- ambiguous identity matches
- conflicting relationship classifications
- unresolved duplicates
- uncertain chronology
- provider disagreements that could materially change auto-inclusion

## Persistence

Extend SQLite rather than creating separate per-media databases.

Likely tables/concepts:

- media_entities
- media_external_ids
- media_relationships
- media_relationship_evidence
- media_releases
- media_watch_policies
- media_graph_snapshots
- media_graph_changes
- media_review_queue

Existing watch records should be migratable/linkable without losing current behavior.

## Acceptance criteria

Phase 1 is complete when:

- Adding a series can automatically create unique logical child works.
- Editions, split volumes, omnibuses, and box sets do not appear as duplicate logical works.
- A direct sequel/prequel series can be discovered automatically from provider evidence.
- Related-but-nondirect series can be retained as discovered candidates.
- Refresh can detect a newly added future installment without user re-entry.
- Provider disagreement is preserved with provenance.
- One unresolved relationship does not halt the rest of an import.
- Existing book/manga/comic/fanfiction watches continue to work.

---

# Phase 2 — Game watching

**Priority: P1**

## Goal

Allow games and game franchises to be added to Watched and automatically monitor releases and related content.

## Candidate provider adapters

- IGDB
- Wikidata
- Steam/store metadata where appropriate
- official publisher/developer pages for authoritative announcements

## Entity coverage

Support:

- franchise
- series/collection
- base game
- expansion
- DLC
- season/content pack
- remake
- remaster
- definitive/complete edition
- port
- sequel/prequel
- spin-off

## Required classification

Do not treat all store products as separate canonical games.

Examples:

- DLC → child release/content of base game
- remaster → related release/version
- remake → distinct work related to original
- deluxe edition → edition/bundle unless it contains genuinely distinct content
- season pass → container/entitlement rather than a game
- soundtrack/artbook → related product, not a game installment

## Watch events

Detect:

- sequel/spin-off announcement
- release-date announcement/change
- DLC/expansion announcement
- launch
- major edition/remaster/remake
- platform release/port
- cancellation
- major update/season when explicitly enabled

## Acceptance criteria

- Adding one game can discover its franchise/collection.
- Direct sequels/prequels can be automatically added by policy.
- DLC/expansions are distinguishable from standalone games.
- Remasters/remakes/ports do not create misleading numbered installments.
- Future releases can be discovered on refresh.

---

# Phase 3 — Video watching

**Priority: P2**

Movies, television, web series, and other video-first formats belong to one **Video** media family. They should share the same core provider routing, entity model, relationship graph, release watcher, UI filters, and deduplication logic.

The app may expose subtype filters such as Movie, TV, Web Series, Short, Special, or OVA, but these are subtypes of Video rather than separate top-level media systems.

## Goal

Allow a user to add any recognizable video title and have Media Intake automatically determine its subtype, discover its franchise/continuity, expand its internal structure where applicable, and monitor future releases or related video works.

Examples:

- a film → collection/franchise → sequels, prequels, spin-offs
- a TV show → seasons → episodes → specials → sequel/spin-off shows
- a web series → playlists/feeds → seasons/arcs → episodes
- an anime title → seasons/cours → episodes → films/OVAs/specials when they belong to the same continuity
- a short film or streaming special → parent franchise/series when one exists

## Shared Video provider layer

Candidate providers should be routed by subtype while normalizing into the same internal Video model.

### General / cross-subtype

- Wikidata
- TMDB
- first-party studio/network/streamer/creator sources

### TV / episodic

- TMDB
- TVmaze
- Wikidata
- official network/streamer schedules

### Web series

- RSS/Atom
- YouTube channel/playlist metadata where available
- Wikidata
- creator/site metadata
- supported page adapters

Additional providers can be added later without changing the Video entity model.

## Shared Video entity coverage

Support at minimum:

- video franchise/universe
- film
- show/series
- web series
- miniseries
- season
- cour/arc where useful
- episode
- short
- special
- OVA/OAD
- direct-to-video release
- streaming-original video
- sequel
- prequel
- spin-off
- revival
- reboot
- remake
- alternate cut/edition
- announced/untitled project
- playlist/feed as a membership source rather than necessarily a creative work

## Shared Video relationship behavior

Normalize relationships such as:

- contains / part_of
- season_of
- episode_of
- special_of
- sequel_to / prequel_to
- spin_off_of
- crossover_with
- same_universe
- revival_of
- reboot_of
- remake_of
- alternate_cut_of
- adaptation_of

A reboot/remake should remain connected to the earlier property without being silently inserted into the same continuity.

A film that is canonically part of a TV/anime continuity should be allowed to share that continuity even though its subtype is Movie.

## Shared Video watch policy

Allow policy inheritance at:

1. video-family defaults
2. subtype defaults
3. franchise
4. series/show
5. season
6. individual work/episode

Example controls:

- watch future films
- watch new seasons
- watch every episode
- watch specials/OVAs
- watch direct sequel/spin-off shows
- discover crossovers/shared-universe branches
- ignore recap/talk/bonus episodes
- track streaming/home-media availability
- track remakes/reboots as related only

## Shared Video watch events

Detect where supported:

- sequel/prequel/spin-off announcement
- new season
- new episode
- new special/OVA/short
- renewal
- cancellation/end
- revival/reboot/remake
- title change
- release/premiere date
- schedule change or delay
- production-status change when reliable
- theatrical release
- streaming release
- home-media release when enabled
- finale/completion
- hiatus/return where reliable
- feed/playlist migration
- newly linked franchise/continuity entry

## Video subtype A — Movies

Movies are a Video subtype, not a separate top-level watcher.

### Movie-specific behavior

Support:

- theatrical films
- streaming films
- direct-to-video films
- franchise collections
- sequels/prequels
- spin-offs
- remakes
- reboots
- alternate cuts/editions
- announced/untitled projects

### Movie acceptance criteria

- Adding a movie can discover its collection/franchise.
- Future sequels are added/discovered according to policy.
- Remakes/reboots remain separate continuity branches.
- Release changes update existing records rather than create duplicates.

## Video subtype B — TV / episodic series

TV, anime series, streaming episodic shows, and similar scheduled episodic video belong to the same Video family.

### TV-specific behavior

Support:

- show/series
- miniseries
- season
- cour where useful
- episode
- special
- sequel series
- prequel series
- spin-off
- revival
- reboot

Allow users to watch only new seasons or every episode without changing the global Video behavior.

### TV acceptance criteria

- Adding a show can discover seasons and related shows.
- New seasons appear automatically.
- Episodes can be watched without forcing episode-level tracking globally.
- Spin-offs/revivals/reboots are classified correctly.
- Provider schedule changes update the same release record.

## Video subtype C — Web series

This subtype should build on the app's existing RSS/Atom web-serial and fanfiction infrastructure where useful, while keeping video-specific episode semantics separate from text serial downloads.

### Web-series sources

Prefer structured sources first:

- RSS/Atom
- YouTube playlists/channels where available
- creator-hosted feeds
- Wikidata
- creator/site metadata

Use site-specific scraping only where permitted and necessary.

### Web-series episode identification

Use combinations of:

- playlist membership
- feed membership
- explicit series metadata
- title patterns
- descriptions
- publication order
- season/episode numbering
- source-provided relationships

Do not equate every creator upload with an episode.

Ambiguous uploads should become candidates rather than being silently injected into the canonical episode list.

### Web-series acceptance criteria

- A structured web series can baseline existing episodes and watch only new ones.
- Playlists/feeds can become membership sources.
- Unrelated creator uploads are not automatically classified as episodes.
- Sequel/spin-off web series can participate in the same continuity graph as books, games, movies, and TV when evidence exists.

## Video subtype D — Other video-first formats

Keep the schema open for additional Video subtypes without requiring a new top-level system.

Examples:

- short films
- anthology segments
- specials
- OVAs/OADs
- independent animation
- creator serials
- documentary series
- concert/performance video series
- video podcasts with structured seasons/episodes
- limited-run streaming projects

The resolver should classify these using the same Video graph and only add subtype-specific fields where needed.

## Phase 3 acceptance criteria

Phase 3 is complete when:

- Movie, TV, Web Series, and other video-first works share one top-level Video family.
- A single Add Anything flow can infer the appropriate Video subtype.
- Cross-format continuity works correctly, such as a TV series with a canonical movie sequel or an anime with OVAs and films.
- Common franchise relationships are normalized once instead of separately for movies/TV/web series.
- New releases are monitored at the correct granularity for each subtype.
- Adding future Video subtypes does not require another independent watch subsystem.

---

# Cross-phase infrastructure

These should be implemented once and reused by every media type.

## Entity resolution

Resolve records using combinations of:

- external provider IDs
- title aliases
- creator
- release year/date
- ISBN or equivalent medium identifier
- parent series/franchise
- source URLs

Store aliases rather than repeatedly creating near-duplicate entities.

## External-ID registry

One logical entity should be able to retain many provider IDs. Only relevant providers are populated.

## Temporal claims

Do not model volatile metadata as a single overwritten value.

Store observation history for fields such as:

- release date
- title
- status
- series position
- relationship
- publisher/studio

A newer/high-authority source may supersede the current canonical value while older observations remain auditable.

## Scheduling

Reuse the existing overdue-watch behavior:

- checks run while the GUI is open
- missed checks resume on next start
- refresh cadence can vary by entity type
- unreleased/upcoming entities may be checked more frequently near release
- completed/static entities can be checked less often

## Notifications / change summaries

Summarize meaningful changes rather than every metadata refresh.

Examples:

- New direct sequel series discovered
- Book 6 received a title
- Game expansion announced
- Movie delayed from May to November
- Season 4 added
- New web-series episode released

## Review queue

Every phase should use the same review system.

Possible reasons:

- ambiguous entity
- conflicting sources
- uncertain relationship
- suspected duplicate
- unsupported/unknown relationship type
- source unavailable
- manually overridden value conflicts with new evidence

## Tests

Build fixture-driven tests for each resolver/provider.

At minimum test:

- duplicate collapse
- split editions
- collections/omnibuses
- sequel/prequel graph traversal
- spin-off discovery without auto-inclusion
- circular relationship protection
- traversal depth limits
- provider disagreement
- future installment discovery
- graph diff idempotency
- migration of existing watched records

Live providers should not be required for the normal unit test suite.

---

# Suggested implementation sequence

## Milestone 1 — Graph foundation

- [x] Database migration and provider-neutral entity/external-ID tables
- [x] Normalized relationships with confidence and evidence provenance
- [x] Graph snapshots and idempotent graph diffs
- [x] Review queue primitives with deduplication and resolution states
- [x] Migration path linking existing watches without merging uncertain identities
- [x] Wikidata search/import path surfaced from the Watched tab

Wikidata discovery stores linked entities and source claims in the graph. A selected
series remains one Watched row; direct book membership is graph metadata. The series
title is searched through connected book sources, and Goodreads list refreshes update
the graph without creating per-book watches.

## Milestone 2 — Book franchise proof of concept

Status: In progress. Wikidata and Goodreads can populate a watched series' explicit
`contains` relationships. Series selection creates one Watched item, and the series
title is searched through connected book sources. Goodreads refreshes update the graph
without creating child watches. Sequel-series traversal and provider disagreement
handling remain open.

Use the existing book stack to prove the architecture.

Target cases:

- The Stormlight Archive
- Percy Jackson & the Olympians

Prove:

- work vs edition/container deduplication
- series expansion
- sequel-series discovery
- related-continuity discovery
- future installment refresh

Current behavior deliberately keeps discovered members in the graph rather than
creating one Watched row per book when a series is selected.

## Milestone 3 — Unified GUI workflow

Status: In progress. The Search and add dashboard now routes category searches to
all available providers for Books, Comics, Manga, Anime, Videos, Games, Light novels,
Web serials, and Fanfiction URL lookup. Per-category full watch support and future
release connectors remain incomplete.

- [x] Category-based Add Anything search routing
- [ ] Add Anything automatic media-type/provider routing
- automatic media-type/provider routing
- import summary
- continuity/related panel
- watch-policy editor
- Needs Review

## Milestone 4 — Games

- IGDB/Wikidata adapters
- game-specific relationship normalization
- release/DLC monitoring

## Milestone 5 — Video foundation

- unified Video media family and subtype model
- shared TMDB/Wikidata provider normalization
- cross-format continuity relationships
- common Video release/status watcher

## Milestone 6 — Movie subtype

- collections/franchises
- sequel/prequel/spin-off discovery
- reboot/remake/alternate-cut handling
- theatrical/streaming/home-media release tracking

## Milestone 7 — TV / episodic subtype

- TMDB/TVmaze/Wikidata
- seasons/episodes/specials/cours
- renewal/cancellation/schedule monitoring
- cross-format movie/special relationships

## Milestone 8 — Web-series subtype

- extend existing feed/web-serial infrastructure
- playlist/feed membership
- episode classification
- web-series continuity relationships

## Milestone 9 — Additional Video subtypes

- shorts/specials/OVAs/OADs
- independent animation
- documentary/performance/video-podcast series where structured
- subtype-specific adapters only when shared Video primitives are insufficient

---

# Definition of done for the roadmap

The long-term target is that a user can add one recognizable piece of media and Media Intake can:

1. identify what it is,
2. discover its larger series/franchise/continuity,
3. normalize the relevant works/releases,
4. collapse redundant editions/containers,
5. automatically add high-confidence continuations according to policy,
6. retain weaker related-media discoveries without polluting the main watch list,
7. watch the graph for future additions and release changes,
8. explain where every relationship came from,
9. continue processing even when a small subset needs manual review,
10. apply the same behavior consistently across books, games, and the unified Video family (movies, TV, web series, anime/video specials, and future video-first subtypes).


---

# Metadata aggregator registry and content-source expansion

**Priority: P0 architectural capability; individual providers are prioritized below.**

Media Intake should treat any legitimate service, database, feed, catalog, index, or API that aggregates useful media metadata as a potentially addable **metadata aggregator provider**. Do not hard-code a short closed list of sources. Maintain a provider registry so new aggregators can be enabled without redesigning media-family logic.

The purpose of this registry is broader than search. Metadata aggregators can improve identity resolution, external-ID mapping, work-vs-edition collapse, franchise/series expansion, relationship evidence, release monitoring, availability discovery, artwork, recommendations, user-state import, and discovery of actual legally accessible content.

## Provider capability contract

Every metadata/content provider should advertise capabilities rather than being treated as globally authoritative.

Suggested capability flags:

- `search`
- `entity_lookup`
- `bulk_catalog`
- `external_id_mapping`
- `aliases`
- `creator_metadata`
- `series_membership`
- `franchise_relationships`
- `continuity_relationships`
- `work_edition_grouping`
- `release_grouping`
- `season_episode_structure`
- `chapter_structure`
- `release_calendar`
- `future_release_discovery`
- `incremental_changes`
- `availability_lookup`
- `streaming_availability`
- `user_library`
- `user_history`
- `ratings`
- `recommendations`
- `covers_artwork`
- `direct_content`
- `download_links`
- `rss_atom`
- `opds`
- `oai_pmh`
- `activitypub_federation`

Provider configuration should also record authentication type, cost/free tier, rate limits, terms/licensing constraints, attribution requirements, cache policy, supported media types, geographic restrictions, API stability, and whether the source is suitable for metadata only versus direct-content acquisition.

## Field-level authority

No provider should be globally canonical. Allow authority/confidence to vary by field and relationship type.

Examples:

- TVmaze may outrank a general movie database for episode air dates.
- Open Library may outrank a social catalog for ISBN/edition identity.
- BookBrainz may provide stronger explicit edition-group relationships.
- Wikidata may be stronger for cross-media identifiers and adaptation relationships.
- a first-party publisher/studio/developer source should outrank aggregators for authoritative announcements.
- Podcast RSS should outrank a third-party podcast index for the canonical episode enclosure URL.
- a provider's explicit relation should outrank a title-similarity inference.

## P0/P1 metadata aggregator candidates

The following should be represented in the provider registry as addable supported metadata aggregators even when an adapter is not implemented yet.

### Cross-media / universal identity

- **Wikidata** — cross-media entities, aliases, identifiers, creators, adaptations, franchises, relationships.
- **Wikipedia / MediaWiki APIs** — human-curated context, page relationships, infobox-derived identifiers where useful; lower authority than structured first-party metadata for volatile fields.
- **Apple Search API** — broad discovery across music, movies, TV, podcasts, audiobooks, ebooks, and apps; useful generic fallback resolver.
- **Internet Archive metadata/search APIs** — cross-format records covering books, audio, video, software, images, collections, and archival items; also a potential direct-content source when rights/availability permit.

### Books / written works

- **Open Library** — work/edition separation, ISBNs, authors, subjects, covers, availability, recent changes, and bulk data.
- **BookBrainz** — authors, works, edition groups, editions, aliases, identifiers, and explicit relationships.
- **Hardcover** — modern book catalog and series/community metadata through GraphQL.
- **Google Books** — book/edition identifiers, ISBNs, publisher metadata, descriptions, covers, and broad search coverage.
- **ISFDB** — speculative-fiction bibliographic, series, publication, award, and title relationships; adapter feasibility/terms should be evaluated before implementation.
- **WorldCat/OCLC** — bibliographic and library holdings metadata where accessible under supported terms/API access.
- **LibraryThing** — supplemental book/edition/series/community metadata where API access permits.
- **Goodreads** — retain import/export and existing supported paths, but do not depend on new public API access.
- **StoryGraph** — candidate import/export or future API integration; do not treat a currently unavailable public API as a dependency.

### Scholarly / technical / research publications

- **OpenAlex** — graph of scholarly works, books, chapters, papers, authors, sources, institutions, topics, open-access locations, citations, and identifiers.
- **Crossref** — DOI bibliographic metadata, publishers, journals, books/chapters, licenses, funding, ORCID/ROR identifiers, updates.
- **DataCite** — DOI metadata for datasets, research outputs, software, publications, and related identifiers.
- **PubMed / NCBI E-utilities** — biomedical publication metadata, links, identifiers, abstracts where available, and bulk updates.
- **PubMed Central** — open-access full-text relationship and content discovery for eligible works.
- **Europe PMC** — biomedical/life-science metadata, citations, grants, preprints, and open-access/full-text discovery.
- **arXiv** — structured preprint metadata/feed integration.
- **DOAJ** — open-access journal/article discovery and metadata.
- **DOAB** — open-access book metadata via bulk exports/OAI-PMH/RSS/Atom.
- **OAPEN** — open-access scholarly books with JSON/CSV/ONIX/KBART/OAI-PMH metadata and full-text linkage.

### Comics / manga

- **Metron** — comic series, issues, arcs, characters, creators, universes, cover hashes, Comic Vine IDs, and Grand Comics Database IDs.
- **Grand Comics Database (GCD)** — issue/series/publisher/creator metadata; use supported data/API/export mechanisms.
- **Comic Vine** — volumes, issues, characters, teams, concepts, publishers, people, and related comic metadata where API terms permit.
- **MangaUpdates** — manga series, type, publishers, licensing, status, and related metadata.
- **MangaDex** — manga, authors, artists, groups, chapters, relationships and multilingual release metadata through its API.
- **AniList** — manga as well as anime; especially useful for relations and adaptation graph evidence.
- **MyAnimeList via supported API/Jikan** — supplemental manga/anime metadata. Treat Jikan explicitly as a third-party MAL-backed aggregator rather than first-party MAL authority.
- **Kitsu** — anime/manga discovery, identifiers, relationships, categories, and user-state opportunities.

### Anime

- **AniList**
- **MyAnimeList / Jikan**
- **Kitsu**
- **AniDB** — anime-specific IDs, episodes, relations, creators and release metadata where API access/terms allow.
- **Anime-Planet** — candidate supplemental catalog/user-list integration if a supported structured path becomes available.
- **LiveChart** — seasonal/release-calendar enrichment where permitted.
- **Simkl** — anime plus TV/movie metadata and valuable external-ID mapping among AniList, MAL, AniDB, Kitsu, TMDB, IMDb, TVDB, and other ecosystems.

### Movies / TV / episodic video

- **TMDB** — movie/TV search, collections, people, images, release metadata, recommendations, external IDs.
- **TVmaze** — show/season/episode structures, specials, alternate episode orders, broadcast and web schedules, external-ID lookup.
- **TheTVDB** — series, movies, seasons, episodes, artwork, translations, statuses, and update streams.
- **Trakt** — movies/shows plus watchlist/history/collection/list state and supplemental metadata.
- **Simkl** — TV/movies/anime plus cross-provider identifier resolution and user watch state.
- **OMDb** — lightweight IMDb-linked movie/TV/episode metadata where API access is appropriate.
- **IMDb datasets/API** — identifier/ratings/title metadata; distinguish free public datasets from licensed commercial API products.
- **Watchmode** — movie/TV metadata plus streaming-provider availability, deeplinks, episode availability, ID mapping, and daily change data.
- **JustWatch** — candidate availability provider only through supported/authorized integration; do not depend on undocumented private endpoints.
- **official network/studio/streamer sources** — authoritative release/status evidence when structured feeds or APIs are available.

### Web video / creator video

- **YouTube Data API** — channels, videos, playlists, playlist membership, publication metadata and structured web-series discovery.
- **PeerTube** — federated video, channels, playlists, search/index federation, ActivityPub relationships.
- **Vimeo API** — videos, channels, showcases/albums, folders, creators and search.
- **Dailymotion API** — videos, users/channels, playlists and structured publication metadata.
- **RSS/Atom video feeds** — generic first-class provider for sites/creators exposing feeds.
- **ActivityPub** — generic federated-content discovery capability where the originating platform exposes useful structured objects.

### Games

- **IGDB** — primary game/franchise/collection/release metadata candidate.
- **RAWG** — broad games, platforms, stores, DLC, developers, publishers, genres and release metadata.
- **MobyGames** — deep historical releases/platforms plus groups that can encode game series, remakes, inspirations and other useful relationships.
- **Steam Web APIs/store metadata** — release/platform/store and DLC/application relationships where supported.
- **GOG catalog/store metadata** — candidate store/release enrichment where supported.
- **Giant Bomb** — games, franchises, characters, concepts and related metadata where API access permits.
- **SteamGridDB** — artwork/identity enrichment, not canonical work metadata.
- **PCGamingWiki** — candidate compatibility/platform/technical metadata source where supported terms allow structured integration.
- **HowLongToBeat** — candidate enrichment only if an authorized structured integration becomes available; do not depend on scraping.

### Visual novels

- **VNDB** — P0 specialist provider for visual novels, releases, languages, platforms, producers, staff, characters, tags, official relations, development status and external links.
- **IGDB/Steam** — secondary identity/release sources for visual novels distributed as games.
- **AniList/MAL/Kitsu** — adaptation/related-anime evidence where applicable.

### Tabletop / board / RPG media

- **BoardGameGeek** — board games, expansions, families, designers, publishers, versions and user collections via supported API.
- **RPGGeek** — tabletop RPG publications and related entities where the BGG API surface supports them.
- **VideoGameGeek** — supplemental game records where useful.
- **Open5e** — open tabletop rules/content metadata and searchable open material.
- **publisher SRDs/open compendia** — direct structured metadata/content sources for licensed/open tabletop material.

### Music

- **MusicBrainz** — artists, recordings, releases, release groups, works, labels, relationships and external IDs.
- **Discogs** — artists, labels, releases and master releases for edition/release grouping.
- **Spotify Web API** — artists, albums, tracks, shows, episodes and audiobooks; metadata/user-library integration only under Spotify policy, never downloading/ripping.
- **Apple Music/iTunes Search** — music plus cross-media catalog discovery.
- **Deezer API** — supplemental artist/album/track/catalog metadata where current API access permits.
- **Last.fm** — artists/albums/tracks, tags and listening/scrobble metadata.
- **ListenBrainz** — listening history, MusicBrainz-linked data and user-history import/export.
- **AcoustID** — audio fingerprint-to-recording identification for locally owned/imported files.
- **Cover Art Archive** — MusicBrainz-linked cover art.
- **Internet Archive audio collections** — archival/open audio metadata and eligible content discovery.

### Podcasts / audio serials / audio drama

- **Podcast Index** — podcast/feed/episode discovery and canonical feed identifiers.
- **Listen Notes** — broad podcast/episode search as a secondary discovery source.
- **Apple Podcasts/iTunes Search API** — cross-platform podcast discovery and IDs.
- **Spotify podcast metadata** — show/episode metadata and user-state opportunities under platform policy.
- **Podcast RSS/Atom feeds** — canonical source for episode membership, GUIDs, publication timestamps and enclosure URLs.
- **OP3-compatible/open podcast analytics identifiers** where useful for identity/analytics enrichment.
- **LibriVox** — public-domain audiobook projects, authors, genres, release updates and direct eligible audio content.

### Audiobooks

- **LibriVox**
- **Spotify audiobook metadata**
- **Apple audiobook catalog/Search API**
- **Open Library** edition/work metadata to connect audiobook editions to logical book works.
- **MusicBrainz** where spoken-word releases are cataloged.
- **Internet Archive** for archival/open audiobook and spoken-word collections.

### Web fiction / fanfiction / serialized text

- **FanFicFare** — treat its supported-site adapter ecosystem as a meta-provider layer for normalized discovery/update metadata across many fiction sites.
- **AO3** — supported page/feed/import adapter; no dependency on an unofficial API as canonical infrastructure.
- **FanFiction.net**
- **FictionPress**
- **Royal Road**
- **Scribble Hub**
- **SpaceBattles**
- **Sufficient Velocity**
- **Questionable Questing**
- **Fiction.live**
- **Wattpad**
- **Tapas**
- **WebNovel**
- **NovelUpdates** — valuable translation/series/release aggregation if a supported structured integration can be used.
- **RanobeDB** — structured light-novel/web-novel series/release metadata.
- **NovelArchive / LNORI** — keep as discovery/catalog adapters subject to existing rights and connector controls.
- **generic RSS/Atom** — first-class serialized-text provider whenever a site exposes a feed.

Site-specific adapters must respect authentication, robots/terms, rate limits, and content rights. Metadata discovery does not itself authorize copying restricted chapter content.

### Public-domain / open ebooks and open scholarly books

These are both metadata aggregators and potential direct-content providers:

- **Project Gutenberg** — daily machine-readable RDF/XML metadata, CSV/MARC catalogs, RSS/OPDS, and public-domain ebook files.
- **Standard Ebooks** — OPDS 1/2 plus RSS/Atom catalog/release feeds and carefully produced public-domain ebooks.
- **DOAB**
- **OAPEN**
- **Internet Archive / Open Library availability links**
- **HathiTrust** — metadata/rights-aware discovery where API access permits; content acquisition must respect item rights.
- **Google Books** — metadata plus preview/full-view availability flags, not a generic downloader.
- **institutional repositories using OAI-PMH** — generic open-publication discovery source.

### Images / open visual media / audio assets

- **Openverse** — open-licensed image and audio aggregation with source/provider/license metadata.
- **Wikimedia Commons** — media files, categories, structured metadata and licenses through MediaWiki APIs.
- **Flickr API** — image metadata where licensing and API terms permit.
- **Unsplash/Pexels** — candidate visual enrichment sources under their API/attribution rules; not canonical media-work metadata.
- **Internet Archive images/collections** — archival/open visual items where rights permit.

## Actual-content aggregation layer

Metadata discovery and content acquisition must remain separate capabilities. A provider may say an item exists without granting Media Intake permission to download or copy it.

Build a generic content-source layer capable of ingesting explicitly legal/authorized content from:

- HTTP(S) direct files
- RSS/Atom enclosures
- Podcast RSS
- OPDS 1.x and OPDS 2.x
- OAI-PMH records that expose open-access locations
- Internet Archive files/derivatives when item rights allow access
- Project Gutenberg catalog/file links
- Standard Ebooks feeds/files
- LibriVox audio
- DOAB/OAPEN open-access full-text links
- PubMed Central / Europe PMC open-access full text where licensing permits
- arXiv source/PDF links
- PeerTube public video objects
- Wikimedia Commons
- Openverse-provided licensed media URLs
- creator/publisher feeds and first-party direct downloads
- user-authenticated personal libraries when an official API/connector permits access
- local files/folders and Calibre/OPDS servers

Do not infer download permission merely from metadata availability. Preserve license/rights evidence, source URL, acquisition timestamp, checksum, and terms/provenance for every acquired item.

## Generic protocol adapters

Prioritize protocol-level adapters because one implementation can unlock many sources:

1. **RSS / Atom**
2. **OPDS 1.x / OPDS 2.x**
3. **OAI-PMH**
4. **ActivityPub**
5. **MediaWiki API**
6. **Schema.org / JSON-LD embedded metadata**
7. **OpenGraph / standard HTML metadata**
8. **MARC / MARCXML**
9. **ONIX**
10. **Dublin Core**
11. **Crossref/DataCite DOI resolution**
12. **generic sitemap + structured-page discovery where allowed**

A source using one of these protocols should often be addable declaratively through configuration before requiring bespoke Python code.

## Provider registry persistence

Add provider-registry concepts such as:

- `media_providers`
- `media_provider_capabilities`
- `media_provider_media_types`
- `media_provider_credentials`
- `media_provider_rate_limits`
- `media_provider_terms`
- `media_provider_field_authority`
- `media_provider_health`
- `media_provider_sync_state`

Keep secrets out of Git/repository configuration. Store only credential references and provider setup metadata.

## Provider health and graceful degradation

The resolver must continue working when one aggregator is unavailable.

Track:

- last successful request
- last error
- latency
- rate-limit state
- authentication state
- schema/API version
- degraded/disabled state
- stale-cache age

A failed provider should reduce evidence coverage, not halt the entire Add Anything flow.

## Research backlog rule

When future research discovers a service that aggregates useful metadata for any supported or plausible media type, add it to the provider registry backlog unless it is clearly unusable, unlawful to integrate, or technically irrelevant. Record why it was rejected rather than silently forgetting it.



## Cultural-heritage super-aggregators and distributed collections

These providers can dramatically increase cross-format discovery because each aggregates many libraries, museums, archives, universities, broadcasters, and cultural institutions rather than representing only one catalog.

- **Digital Public Library of America (DPLA)** — P0/P1 US cultural-heritage aggregation. Search items and collections across many contributing repositories; preserve contributing institution, source URL, rights data, and DPLA IDs. DPLA bulk data should be considered for local indexing where appropriate.
- **Europeana** — P0/P1 European cultural-heritage aggregation across books/text, images, audio, video, artworks and museum/archive objects. Support Search/Record APIs, rights/reusability filters, external provider links, SPARQL/OAI-PMH where useful, and media links when rights permit.
- **Smithsonian Open Access** — P1 museum/archive/library metadata with open-access media links for eligible public-domain objects. Preserve Smithsonian unit/collection provenance and rights.
- **Library of Congress loc.gov API** — P1 cross-format discovery for digitized books, audio, film/video, maps, newspapers, manuscripts, photographs, web archives and notated music. Also investigate MARC/linked-data routes for broader bibliographic identity.
- **Trove (National Library of Australia)** — P1 cross-format aggregator for books, newspapers/magazines, images, research, music, diaries, people/organizations and related Australian collections.
- **DigitalNZ** — P1 aggregator across images, audio, video, books, newspapers, manuscripts, research papers, articles, archives, data and web pages. Its API exposes source pointers and rights metadata rather than pretending DigitalNZ owns the underlying content.
- **HathiTrust** — P1 bibliographic/full-view discovery where supported; content access must remain rights-aware.
- **institutional/repository networks** — treat OAI-PMH, IIIF, OPDS and repository APIs as generic sources so individual universities, museums and archives can be added without bespoke code.

### IIIF support

Add **IIIF Presentation API** and **IIIF Change Discovery API** to generic protocol adapters.

IIIF support should allow Media Intake to:

- ingest manifests as structured media/container records
- preserve canvases/pages/sequences
- follow `seeAlso`, `rendering`, `homepage`, `provider`, `requiredStatement` and rights metadata
- discover linked OCR/text/metadata representations
- use Image API services for permitted image retrieval
- use Change Discovery to detect additions, updates and deletions in large collections
- map manifests into collection/item/page relationships without assuming every canvas is a distinct creative work

IIIF is especially valuable because one adapter can unlock many libraries, archives, museums and cultural aggregators.

## Mods / user-created game content as a future Media family

Treat mods, modpacks, curated collections and other creator-distributed game content as a plausible future media/content family rather than forcing them into the base-game schema.

Add provider candidates:

- **Nexus Mods API** — mods, files/versions, games, collections and update metadata. Especially useful for existing Agent OS/mod-research workflows and future Media Intake tracking.
- **Modrinth API** — projects, versions, dependencies, loaders, game versions, categories, licenses, galleries and update dates.
- **CurseForge API** — candidate mod/modpack/addon metadata provider subject to API-key/terms review.
- **Thunderstore API** — candidate package/community/version/dependency metadata provider.
- **itch.io API** — authenticated user-owned/developer catalog integration and proof-of-ownership/download workflows where official API capabilities permit; do not assume it provides a global searchable catalog API.
- **Steam Workshop** — candidate workshop/item metadata via supported Steam APIs where available.

Normalized mod-content entities should support:

- project/mod
- modpack/collection
- file/version
- dependency
- optional dependency
- incompatibility
- game
- game version
- loader/framework
- author/team
- collection/list
- release/update
- archived/deleted release

This family should reuse the same provider registry, relationship graph, release monitoring, rights/provenance, and user-library concepts.

## Research/data super-aggregators

Add these higher-order research aggregators because they already reconcile records from many repositories and identifier systems:

- **OpenAIRE Graph** — publications, datasets, software and other research products aggregated from Crossref, DataCite, DOAJ, repositories, ORCID-linked records and other sources; useful as a deduplicated discovery layer.
- **DataCite** — DOI metadata for datasets, software, publications and other research outputs; public REST API plus OAI-PMH/bulk options.
- **Europe PMC** — life-sciences publications/preprints, open-access/full-text links, references, citations and text-mined entities.
- **LibraryThing thingISBN/thingTitle** — useful specialist work/edition equivalence evidence by mapping ISBNs that belong to the same logical work.
- **WorldCat/OCLC** — valuable bibliographic/holdings source but access is subscription-gated; represent it as an optional enterprise/library provider rather than a default dependency.

## Provider selection policy additions

When multiple layers exist, prefer efficient aggregation before fan-out:

1. Resolve against a high-coverage aggregator/ID bridge.
2. Use returned external IDs to query specialist databases deterministically.
3. Query first-party sources for volatile/release-critical claims.
4. Fan out to weaker fuzzy-search providers only when unresolved fields remain.
5. Cache normalized results and provenance.
6. Preserve provider disagreement instead of selecting a silent winner.

This should reduce API calls while increasing coverage and identity precision.


## Additional open-content and scholarly aggregators

Add the following provider candidates discovered during continued aggregator research.

### Scholarly / open-access expansion

- **CORE** — P0/P1 scholarly aggregator. CORE collects, harmonizes and enriches metadata and full text from thousands of repositories/journals/providers and exposes searchable metadata plus open-access full-text content through its API. Treat metadata lookup and full-text acquisition as separate capabilities and retain the originating repository/provider and license/access provenance.
- **Semantic Scholar Academic Graph** — P1 scholarly identity/discovery provider for papers, authors, citations, references, venues and related IDs. Also expose its Recommendations API as an optional discovery capability and its dataset releases/diffs as a bulk/local-index route when appropriate.
- **Semantic Scholar Datasets** — bulk JSON graph snapshots/diffs should be a distinct provider capability from interactive API search so a future local scholarly index can avoid excessive API fan-out.
- **Unpaywall** — retain as optional DOI-level open-access-status/location enrichment. Do **not** use it as a general search provider: its `/v2/search` endpoint was retired on 2026-09-18 and its own documentation directs search workloads to OpenAlex. Prefer OpenAlex for discovery and use Unpaywall only where its DOI OA record adds useful evidence.

### Open / creator-licensed audio and music

- **Freesound API v2** — P1 audio metadata/content provider for sounds, packs, users, tags, descriptions, formats, licenses, geotags, ratings, similarity and content-analysis/audio descriptors. Capture the per-sound Creative Commons license and any creator-supplied generative-AI preference metadata. Download/acquisition must honor each sound's content license and API terms.
- **Jamendo API** — P1 open/creator music catalog candidate for artists, albums, tracks, playlists/radios and music discovery. Treat direct download as conditional: the API exposes fields such as `zip_allowed` and `audiodownload_allowed`; only expose acquisition when the current record explicitly permits it.
- **Openverse** — retain as the general cross-provider open image/audio discovery layer; preserve upstream source/provider and license and verify current licensing at acquisition time instead of treating aggregation as a guarantee.
- **Wikimedia Commons** — retain as a structured open-media provider and potential direct-content source subject to each file's license.

### Provider lifecycle/deprecation handling

Provider-registry entries need lifecycle state:

- `candidate`
- `active`
- `degraded`
- `deprecated`
- `retired`
- `rejected`

Track replacement/successor providers and endpoint-level retirement dates, not just whole-provider status. A service can remain useful for ID lookup while a search endpoint has been retired.

Known example:

- Unpaywall DOI lookup: active enrichment.
- Unpaywall general search: retired 2026-09-18; successor route = OpenAlex works search.

Do not add dead APIs as live supported providers merely because historical documentation exists. Keep a rejected/retired-provider ledger so future research does not repeatedly rediscover and attempt to integrate the same dead endpoint.


## Continued aggregator research — serialized media, personal libraries, news, franchise data, and preservation

### Webcomics / manga / serialized visual stories

- **MangaDex API** — P0/P1 structured manga provider. Support manga search, authors/artists, tags, multilingual titles/descriptions, chapters, scanlation groups, cover art, publication status, and explicit manga-to-manga relations such as sequel/prequel/spin-off/alternate-story relationships. Treat hosted chapter/page acquisition as a separate capability subject to MangaDex API/acceptable-use rules and the individual content's status.
- **ComicFury RSS** — P1 generic webcomic update source. ComicFury comics expose RSS feeds, including comic-only feeds, making it suitable for baseline + incremental page/update tracking without bespoke scraping.
- **generic webcomic RSS/Atom** — first-class content/update adapter for independent creator sites.
- **WEBTOON** — candidate metadata/page adapter only if a supported public or authorized integration route is available; do not depend on undocumented internal endpoints.
- **Tapas** — candidate metadata/page adapter only if a supported public or authorized route is available.
- **Hiveworks / SpiderForest / independent webcomic collectives** — candidate discovery sources through site indexes, per-comic RSS/feeds, sitemaps, and structured page metadata where available.
- **Webcomic archive pages + Schema.org/OpenGraph** — generic declarative adapter for sites with clear archive navigation but no dedicated API, subject to site terms and rate limits.

### Asian web novels / light novels

- **Shōsetsuka ni Narō / Syosetu Developer API** — P0 Japanese web-novel metadata provider. The official API exposes works, authors, genres, keywords, publication/update timestamps, chapter count, length, completion/stop state, rankings and Atom output. Use N-code as a durable external ID.
- **Syosetu Atom feeds** — incremental update source for tracked works.
- **Kakuyomu** — candidate page/feed provider. Prefer publisher/creator feeds or structured metadata if exposed; no undocumented internal API dependency.
- **Royal Road RSS** — P0/P1 serialized-fiction update provider. Individual fiction syndication feeds expose recent chapter updates, and multiple fiction IDs can be combined in one syndication feed.
- **NovelUpdates** — valuable translation/release aggregation candidate when a supported structured path is available; retain as metadata/discovery-only until integration terms are clear.
- **FanFicFare-supported Asian fiction sites** — use the FanFicFare meta-provider where it already has maintained adapters instead of duplicating parsing logic.

### Podcasts / audio drama / radio

- **Podchaser API** — P1 rich podcast aggregator. Supports podcast/episode search plus creator/guest credits, networks, transcripts, charts, similar podcasts, lists and audience metadata. Support REST for smaller integrations and optional GraphQL/bulk delivery for higher-volume configurations.
- **Radio Browser API** — P0/P1 open internet-radio directory. Public API exposes station metadata, stream URLs, homepage/favicon, tags, country/state/language, codecs/bitrates, health status, votes/clicks and multiple playlist output formats (M3U/PLS/XSPF). Treat live stream URLs as playable sources, not downloadable permanent media.
- **Podcast Index** — retain as the open podcast/feed identity layer and canonical RSS resolver.
- **Listen Notes** — retain as broad full-catalog podcast/episode search.
- **audio-drama-specific directories** — candidate enrichment providers when a documented API/feed exists; map audio dramas into the same podcast/serial graph rather than creating a disconnected system.

### News, magazines, newspapers, and periodicals

Add a future **Periodical / News** media family capable of representing publication → issue → article/page relationships.

Provider candidates:

- **Library of Congress Chronicling America via loc.gov API** — P0 historical-US-newspaper provider. Exposes title, issue and page-level search, OCR/full text, IIIF image services and structured JSON/YAML without an API key. The legacy dedicated Chronicling America API was retired in favor of loc.gov in 2025, so use the current loc.gov route.
- **New York Times Archive API** — P1 optional publication-specific provider. Month-by-month article metadata back to 1851; requires API key and must respect NYT API terms/attribution.
- **GDELT 2.x** — P1 global news discovery/monitoring aggregator. Useful for finding articles/events across many publishers and generating topic-specific RSS/JSON feeds; treat discovered publisher URLs as provenance, not as permission to copy article bodies.
- **NewsAPI** — optional commercial/general-news discovery provider. Search recent articles across a large source set; metadata/link discovery only unless the originating publisher independently permits content ingestion.
- **Trove newspapers/magazines** — retain as a cultural-heritage periodical provider.
- **Europeana/DPLA/Internet Archive periodicals** — reuse the cultural-heritage layer for historical magazines, journals and newspapers.
- **publisher RSS/Atom feeds** — canonical incremental source for publications that expose them.
- **Crossref/OpenAlex** — bridge scholarly journals/articles into the same periodical model where applicable.

Normalize:
- publication
- volume
- issue
- edition
- section
- article
- page
- supplement
- column/series
- publication date
- correction/retraction/update
- canonical URL
- DOI/ISSN/LCCN/OCLC identifiers where available

### Personal/self-hosted media library aggregators

Treat user-owned library servers as a separate high-trust provider class. They aggregate content the user already has and can provide exact local identity, hierarchy, file paths, playback state and collections.

- **Plex Media Server API** — P0 personal-library provider for movies, TV, music and other Plex library media. Import libraries, hierarchy, GUID/external IDs, collections, related/similar items, artwork, file metadata and watch state where authorized. Plex's newer metadata-provider API should also be evaluated as a way for Media Intake itself to become a Plex metadata provider.
- **Jellyfin** — P0/P1 personal-library provider. Libraries support movies, shows, music, books, photos and mixed media; integrate through the supported server API and preserve Jellyfin item IDs plus external provider IDs.
- **Kavita API** — P0/P1 personal ebook/comics/manga library provider. Official OpenAPI/Swagger surface and API-key authentication make it suitable for library, series, volume/chapter, reading-progress and collection import.
- **Komga API** — P0/P1 comics/manga/ebook server provider with official REST/OpenAPI, API keys/basic auth, series/books/collections/read-progress metadata.
- **Audiobookshelf API** — P0/P1 audiobook/podcast personal-library provider. Supports book and podcast libraries, authors, series, chapters, RSS feed links, playlists/collections and listening progress. Use the newer API-key/JWT authentication model and track API version because older static docs are deprecated.
- **Navidrome / OpenSubsonic** — P1 personal music-library provider. OpenSubsonic/Subsonic-compatible APIs expose libraries, artists, albums, songs, genres, playlists, favorites, play queues, bookmarks, ratings/scrobbles, cover art and downloads from the user's own server.
- **generic Subsonic/OpenSubsonic server adapter** — one protocol adapter should support Navidrome and other compatible music servers.
- **Calibre Content Server / OPDS** — retain as P0/P1 personal ebook-library source.
- **local filesystem + embedded metadata** — continue supporting file/folder import with hashes and embedded tags, then reconcile against public providers.

Personal-library providers should have capabilities such as:
- `owned_content`
- `local_file_identity`
- `playback_progress`
- `reading_progress`
- `user_rating`
- `collection_membership`
- `playlist_membership`
- `download_from_owned_server`

### Indie/open music expansion

- **Audius API / SDK** — P1 open music catalog/provider for artists/users, tracks, albums/playlists, discovery/search, streaming and user libraries. Its free developer tier is large enough for normal personal use; treat stream/download semantics according to current Audius content/API rules.
- **Bandcamp API** — optional **account/sales/provider-side** integration only. The documented API is aimed at labels and fulfillment partners and is not a public global-catalog search API, so do not use undocumented endpoints as a general Bandcamp catalog dependency.
- **Jamendo** — retain as the open/creator music catalog with explicit `audiodownload_allowed` / `zip_allowed` enforcement.

### Game catalog and preservation metadata

- **TheGamesDB API** — P1 game metadata provider with title search, platform lookup, developer/publisher/genre/region/country data, images/videos, ROM-hash lookup and update logs.
- **RetroAchievements API** — P1 retro-game identity/user-state provider. Supports game metadata, hashes, achievements, recently played/completion state and want-to-play lists. Useful as both a metadata source and a user-library/progress source.
- **MAME software lists** — P1 machine-readable preservation metadata. MAME XML software lists encode software identity, year, publisher, alternate titles, parent/clone relationships, media parts, CRC/SHA1 hashes and compatibility/support state. Import metadata/hashes only; do not treat preservation metadata as a ROM-content source.
- **No-Intro / Redump / TOSEC / preservation DAT ecosystems** — candidate checksum/identity providers when official redistributable DATs or supported APIs are available. Keep metadata/hashes separate from copyrighted software bytes.
- **Giant Bomb** — mark current API integration as **degraded/unavailable** as of 2026 unless the rebuilt public API returns. Giant Bomb states that its previous Games/Characters/Companies/Concepts/Releases APIs are not currently available while its wiki stack is being rebuilt.
- **IGDB** — strengthen to P0 for game relationship modeling because it directly exposes collections/series, franchises, DLC, expansions, remakes, remasters, ports, standalone expansions, parent games, bundles and version-parent relationships.

### Franchise-specific / universe knowledge providers

Use franchise-specific databases as **supplemental relationship/lore evidence**, never as the sole canonical release database.

- **generic MediaWiki/Fandom adapter** — P1. MediaWiki Action/REST APIs provide search, pages, categories, links, templates, revisions and recent changes across many franchise wikis. Build one configurable adapter rather than a separate scraper per wiki.
- **Memory Alpha** — Star Trek specialist example proving the pattern: it exposes MediaWiki API endpoints, RSS/watchlist feeds and downloadable database dumps. Respect its CC BY-NC license and keep page text/licensing provenance.
- **UESP and other independent MediaWiki franchise wikis** — candidate configurable MediaWiki instances for Elder Scrolls and other universes; verify each site's API/usage/license before activation.
- **PokéAPI** — P1 Pokémon-specific structured knowledge provider for generations, game versions, species, moves, abilities, types, locations and other game-world entities. Useful for franchise entity graphs, not for release tracking alone.
- **SWAPI / maintained Star Wars API implementations** — P2 Star Wars knowledge enrichment for films, people, planets, species, vehicles and starships; treat community APIs as supplemental and validate maintenance/lifecycle.
- **franchise-wiki database dumps** — when a wiki offers current dumps under a compatible license, prefer scheduled bulk import over hammering page APIs for large-scale indexing.

Add capability flags:
- `franchise_lore`
- `characters`
- `locations`
- `organizations`
- `in_universe_timeline`
- `canonicality_claims`
- `wiki_revision_monitoring`

Keep fictional-universe canon claims separate from real-world release/edition relationships.

### Tabletop/RPG follow-up

- **Open5e v2** — upgrade from generic candidate to strong open-content provider. Its current REST API exposes source documents plus rules/resources such as creatures, spells and other open game content. Preserve source-document/license provenance on every imported record.
- **D&D Beyond** — mark as **rejected for direct API integration** unless Wizards/D&D Beyond launches a documented public API. D&D Beyond staff/community documentation continues to state there is no supported public API; do not depend on reverse-engineered internal endpoints.
- **RPGGeek/BoardGameGeek** — retain for publication/expansion/family metadata rather than rules text unless the specific content is licensed/open.

### itch.io discovery refinement

The official itch.io API is account/developer oriented, but **browse pages expose RSS feeds simply by appending `.xml`**. Add an itch.io RSS discovery adapter for:
- free games
- new releases
- tagged/category browse pages
- platform filters
- sales/bundles where represented by supported feeds

Use official authenticated API separately for the user's owned/developer-visible content and purchase proof.

### New generic adapter: user media-server protocol

Create a normalized internal provider interface for self-hosted media servers:

- `library.list`
- `library.search`
- `library.items`
- `library.item.get`
- `library.children`
- `library.collections`
- `library.playlists`
- `library.progress`
- `library.external_ids`
- `library.files`
- `library.sync`

Then map Plex, Jellyfin, Kavita, Komga, Audiobookshelf, Calibre/OPDS and OpenSubsonic onto it where their capabilities overlap.



## Download-capable connector registry

**Priority: P0 architecture, then source-by-source enablement.**

Media Intake should maintain a separate registry of providers that can yield downloadable media files, downloadable archives, authenticated owned-library files, feed enclosures, or direct creator/public-domain assets.

This is intentionally separate from metadata-provider support. A source may support metadata without file acquisition, file acquisition without broad discovery, or both.

Each connector should advertise one or more acquisition modes:

- `public_direct_file`
- `public_collection_download`
- `rss_enclosure`
- `opds_acquisition`
- `authenticated_owned_download`
- `creator_authorized_download`
- `self_hosted_library_download`
- `api_download`
- `browser_session_download`
- `mirror_sync`
- `bulk_dataset`
- `stream_only`
- `preview_only`

Also store authentication requirements, file formats, checksums, source item ID, original filename, canonical source URL, content URL, acquisition method, observed timestamp, exposed license/rights metadata, and whether the connector yielded a direct file, authenticated entitlement, or only a landing page.

The long-term rights/policy layer can decide which acquisition methods are allowed. The connector registry itself should still know what a provider technically exposes.

### Cross-media archives and public collections

- **Internet Archive / archive.org** — P0 download-capable provider. Its official client supports item metadata, file enumeration, per-file downloads, format-filtered downloads, glob downloads, entire-item downloads, entire-collection downloads, checksums, and generated derivatives such as EPUB/MOBI/DAISY. This should become one of the main general-purpose content connectors because an item may contain books, audio, movies, software, images, datasets or mixed collections.
- **Wikimedia Commons** — P0 open-media connector. MediaWiki APIs expose file metadata, original URLs, MIME type, dimensions, SHA-1 and revisions. Support original-file acquisition plus attribution/license provenance.
- **NASA Image and Video Library** — P1 multimedia connector for downloadable images, video, audio, captions and associated metadata. Use the public NASA media API where available.
- **Smithsonian Open Access** — P1 downloadable image/media connector for eligible open-access items.
- **Library of Congress** — P1 direct-content connector where loc.gov item records expose public files, OCR, images, audio/video or IIIF resources.
- **Europeana / DPLA / DigitalNZ / Trove** — aggregator connectors that may resolve to downloadable upstream files. Preserve the originating institution and source URL.

### Ebooks and written media

- **Project Gutenberg** — P0. Prefer OPDS/RDF/CSV/MARC catalogs and the documented robot/mirror download routes rather than crawling normal web pages. Support EPUB, HTML, plain text, Kindle and MP3 where available.
- **Standard Ebooks** — P0/P1 OPDS 1/2 acquisition connector plus release feeds.
- **LibriVox** — P0/P1 audiobook metadata plus downloadable audio files.
- **OAPEN / DOAB** — P1 open scholarly-book connector.
- **arXiv** — P1 paper connector for PDF/source downloads plus Atom metadata.
- **PubMed Central / Europe PMC** — P1 open-access full-text connector for eligible XML/PDF/supplementary files.
- **CORE** — P1 scholarly full-text aggregator where its API exposes open full text.
- **institutional repositories** — generic OAI-PMH/IIIF/direct-file connector for repositories exposing open PDFs, EPUBs, OCR, datasets or supplementary media.
- **Kobo owned library** — optional authenticated-owned-library connector. Kobo supports exporting eligible purchases as EPUB/PDF; some purchases use Adobe DRM/ACSM, so record file/DRM state and do not assume every title yields a directly usable EPUB.
- **DriveThruRPG owned library** — P1 authenticated-owned-library connector for PDFs, EPUBs, ZIPs, STL files, VTT modules and other purchased digital files. Its Library App already syncs library metadata and supports title/file-level downloads.
- **Smashwords/Draft2Digital storefront libraries** — candidate authenticated ebook download connector if a stable supported account/export interface is available.

### Fanfiction / serialized text exports

- **Archive of Our Own (AO3)** — candidate P0/P1 native-export connector. Prefer native AO3 download links over reconstructing works by scraping chapter HTML. Preserve work ID, version/update timestamp and source URL.
- **FanFicFare** — P0 meta-connector for supported fiction sites where it can technically produce EPUB/HTML/TXT from a user-supplied URL; keep site-specific policy/connector controls around it.
- **Royal Road / other serial sites with creator-provided EPUB or RSS** — enable direct acquisition only when the site itself exposes an export/download route; otherwise retain update/metadata support separately.

### Podcasts, audio feeds, and radio

- **Podcast RSS** — P0 direct-content protocol. Parse enclosure URLs, MIME type, duration, GUID, publication date, chapters/transcripts where present, and incremental episode updates.
- **Podcast Index** — use as feed resolver/metadata aggregator, then acquire episodes from canonical RSS enclosure URLs.
- **Internet Archive audio collections** — P0/P1 direct downloadable audio collections where individual item files are exposed.
- **Freesound API** — P1 authenticated API download provider. OAuth2 can download original sound files and complete packs; preserve sound/pack metadata and per-item license.
- **Jamendo** — P1 conditional music-download provider. Only expose direct acquisition for records whose API fields explicitly permit audio/ZIP download.
- **ccMixter** — P1 Creative Commons audio/remix/sample provider. Its site exposes streams/downloads and RSS/Atom-style Sample Pool metadata with CC license information; useful for tracks, samples and stem ZIPs.
- **Musopen** — P1 public-domain/classical recording and sheet-music provider. Supports free downloadable recordings and PDFs.
- **Radio Browser** — `stream_only` connector for live stations; model live stream URLs as streams rather than permanent downloaded works.

### Music storefront / owned-library connectors

- **Bandcamp** — P1 authenticated-owned-library connector. Purchases can be downloaded from the user's collection/purchases pages in MP3 V0, MP3 320, FLAC, AAC, Ogg Vorbis, ALAC, WAV and AIFF. Model this as user-authenticated owned-content retrieval rather than a public catalog API.
- **GOG music/soundtrack extras** — where purchased games expose soundtrack/extras as downloadable files, treat these as child assets of the owned game.
- **Jamendo / Freesound / ccMixter / Musopen** — retain public/creator-authorized acquisition modes described above.

### Video

- **PeerTube** — P0/P1 federated-video direct-content connector. The REST surface exposes public static video file routes in addition to metadata, channels and playlists. Preserve the originating instance and ActivityPub IDs.
- **Internet Archive movies/video** — P0 video file connector with multiple derivatives/formats and captions where available.
- **NASA video library** — P1 downloadable public media, including caption files.
- **Blender Studio/Open Movies and Blender demo files** — P1 downloadable open-movie/project asset connector.
- **creator-hosted RSS/Atom/video feeds** — generic enclosure/direct-link support.
- **Vimeo/YouTube/Dailymotion** — metadata/playback connectors unless the official API/account state explicitly provides a download for that specific item.

### Game stores and owned software libraries

- **GOG** — P0/P1 authenticated-owned-library connector. GOG explicitly provides offline backup game installers and DLC installers from a user's account. Import ownership, versions, platform/language, installer parts, extras and checksums where obtainable.
- **itch.io** — P0/P1 authenticated-owned-library connector. Official API can enumerate owned download keys from purchases, claims, bundles and gifts; use supported authenticated download flows for files tied to those entitlements. Keep browse-page RSS as the public discovery side.
- **Humble Bundle / Humble library** — candidate authenticated-owned-library connector for DRM-free games, ebooks, comics, audio and software from the user's purchases if a stable supported integration/browser-session flow can be maintained.
- **Steam** — library/installation connector only through supported Steam client/API mechanisms.
- **Epic Games Store** — owned-library/installation candidate through official client/platform mechanisms.
- **Internet Archive software collections** — metadata/file connector where item files are publicly exposed.

### Game-development assets / reusable media

- **Kenney** — P1 downloadable game-asset provider. Asset pages expose ZIP downloads, categories, file counts, versions and CC0 licensing; useful for 2D, 3D, UI, textures, audio and development packs.
- **OpenGameArt** — P1 downloadable asset connector. Individual submissions expose files, creator, tags, collections and explicit license fields. Begin with a conservative page/RSS adapter if permitted rather than assuming a mature public API.
- **Freesound** — sound-effect/sample provider as above.
- **Blender demo/open-movie files** — 3D scenes, animation, textures and project files.
- **itch.io asset packs** — public/free discovery via RSS plus authenticated entitlement/download for claimed/purchased packs.
- **Openverse** — discovery layer for open images/audio; follow upstream content URLs while preserving provider/source/license.
- **Wikimedia Commons** — images/audio/video/3D/document files.
- **NASA media** — images/audio/video.

### Sheet music / scores / source media

- **IMSLP** — P1 candidate score/recording connector. IMSLP exposes a MediaWiki API and downloadable score/media files; build metadata discovery through MediaWiki and a separate file-link resolver.
- **Musopen** — P1 downloadable score PDF and recording provider.
- **Mutopia Project** — candidate open/public-domain sheet-music connector; investigate machine-readable indexes and downloadable PDF/MIDI/LilyPond/other source formats.
- **Wikimedia Commons** — score images/PDF/MIDI/audio where cataloged.

### 3D models / printable and creative source files

Treat downloadable creative assets as a supported media subtype even when they are not traditional books/movies/music.

Candidate connectors:

- **Printables**
- **Thingiverse**
- **MakerWorld**
- **Thangs**
- **MyMiniFactory**
- **Cults3D**
- **NASA 3D Resources**
- **Smithsonian 3D/Open Access**
- **Internet Archive 3D items**
- **Wikimedia Commons 3D files**
- **OpenGameArt / Kenney / itch.io asset packs**

Track model/project page, author, license, version/revision, STL/3MF/OBJ/STEP/source files, images, remix/source relationships, print profiles, compatible machine/material metadata when exposed, and download entitlement state.

### Comics and graphic media

- **Internet Archive comics/periodicals** — downloadable CBZ/PDF/image/derived formats when exposed.
- **Digital Comic Museum** — candidate public-domain comic download connector; investigate supported access pattern before implementation.
- **Comic Book Plus** — candidate public-domain/permissioned comic and pulp download connector; investigate supported automation/terms before implementation.
- **MangaDex** — retain metadata/chapter provider separately from acquisition; any page/chapter-file integration should use its documented API/acceptable-use model.
- **DriveThruComics / DriveThruRPG library** — authenticated-owned digital acquisitions where exposed by the user's library.

### Bulk/local mirror sources

Some providers should support optional large-scale synchronization rather than one-file-at-a-time retrieval:

- Project Gutenberg mirrors/harvest routes
- Internet Archive collections via official client/search download
- Wikimedia dumps/media categories where appropriate
- Semantic Scholar/OpenAlex/other bulk metadata datasets
- franchise wiki database dumps
- MAME/software-list XML metadata
- provider-supplied OPDS/OAI-PMH/IIIF Change Discovery feeds

Bulk sync should have storage quotas, resume/checkpoint support, checksums, deduplication and source-level enable/disable controls.

### Acquisition routing

Add a provider-independent interface:

- `media.acquire.plan(entity_id)`
- `media.acquire.sources(entity_id)`
- `media.acquire.resolve(provider_item_id)`
- `media.acquire.list_files(provider_item_id)`
- `media.acquire.download(file_id, destination)`
- `media.acquire.sync_collection(provider_id, collection_id)`
- `media.acquire.verify(file_id)`
- `media.acquire.import(file_id)`

The planner should be able to return multiple routes for the same logical work, such as an open file, owned-store purchase, self-hosted copy, feed enclosure, local-file match or stream-only availability. The metadata graph should deduplicate these routes under the same logical entity instead of creating duplicate media records.


### Open art, museum, 3D, texture, and source-file connectors

Add the following download-capable providers to the connector backlog.

#### Wikisource / Wikimedia text export

- **Wikisource** — P0/P1 ebook export connector. Works expose built-in EPUB, MOBI and PDF downloads in addition to page/source data through MediaWiki. Use the work's top-level page and Wikidata/scan backing where available to preserve logical-work identity and source provenance.
- **Wikibooks / other Wikimedia text projects** — candidate export connectors where the same export infrastructure produces structured downloadable books/documents.

#### Museum/open-art APIs with high-resolution files

- **The Metropolitan Museum of Art Open Access API** — P1 artwork/image connector. Public-domain object records expose downloadable images and structured collection metadata.
- **Art Institute of Chicago API + IIIF** — P1. Public-domain works expose IIIF manifests/images and documented download patterns; support metadata search plus local image acquisition when needed.
- **Cleveland Museum of Art Open Access API** — P1. The API includes metadata and image links for tens of thousands of works, including full-size/uncompressed image paths for eligible CC0 items, plus ZIP JPEG+caption and high-resolution TIFF endpoints.
- **Wellcome Collection** — candidate P1 IIIF/image connector. Investigate current API + IIIF surfaces for images, digitized books, manuscripts and other collection objects.
- **Rijksmuseum / National Gallery / Yale / Getty open-access collections** — candidate museum-specific connectors where current APIs/IIIF expose downloadable public-domain/open-access images. Prefer IIIF generic support whenever possible to avoid bespoke code.

#### 3D / HDRI / texture / production-asset libraries

- **Poly Haven API** — P0/P1 asset connector. Its public API exposes asset search/metadata plus `GET /files/{id}` download URLs, file sizes, hashes and dependencies for HDRIs, textures and models. Support HDR/EXR/JPG, texture maps, Blend/USD/glTF and other listed file variants.
- **ambientCG** — P1 CC0 texture/material/HDRI/model connector. The public asset API exposes structured asset data and direct ZIP downloads; normalize resolutions, maps/material channels and archive contents.
- **Sketchfab Download API** — P1 authenticated downloadable-3D provider. For download-enabled models, OAuth users can request short-lived archive URLs; current API downloads provide formats including glTF/GLB and USDZ. Preserve model UID, creator, license/attribution data, available formats and expiring URL state.
- **NASA 3D Resources** — P1 scientific/space 3D-asset connector.
- **Smithsonian 3D/Open Access** — P1 museum/scientific 3D connector.
- **Quaternius** — candidate downloadable CC0 low-poly/game-asset connector.
- **Objaverse** — candidate large-scale 3D metadata/download aggregator. Use only records whose upstream licensing/source data are retained and verify the current dataset/API route before activation.
- **Google Scanned Objects / research 3D datasets** — candidate downloadable model datasets where official dataset hosting and licensing permit integration.
- **Khronos glTF Sample Assets** — P2 developer/reference connector for structured downloadable glTF test/sample media.
- **Blender Extensions / demo/open-movie resources** — P1 production source-file connector for .blend projects and associated assets.

#### Public scientific and government media

- **NASA Image and Video Library** — strengthen as P1 because its searchable library provides downloadable image/video/audio variants and downloadable caption files.
- **NOAA / USGS / National Park Service media APIs** — candidate P1 scientific/geospatial/public-media connectors where current APIs expose downloadable photography, maps, audio/video or datasets.
- **Library of Congress digital collections** — retain as a broad image/audio/video/text connector and exploit IIIF where available.

#### Game/media asset catalogs without strong APIs

- **Kenney** — direct ZIP asset packs with categories, versions and file counts; suitable for a lightweight catalog/page connector.
- **OpenGameArt** — downloadable 2D/3D/audio/music/archive files with creator, tags, collections and license metadata. Since a mature public search API is not evident, keep it as a page/RSS connector candidate rather than pretending there is a stable API.
- **itch.io asset packs** — combine browse RSS, metadata/page resolution and authenticated claimed/purchased downloads.
- **creator GitHub/GitLab releases** — generic release-asset connector for open media/asset projects whose canonical distribution is a repository release. Store repository, release/tag, asset filename, checksum when available and source license.

#### Additional open audio/music repositories

- **ccMixter** — reinforce as a direct-download remix/sample connector; RSS/Atom-compatible Sample Pool metadata includes Creative Commons license information, and individual works may expose MP3/FLAC/stem archives.
- **FreePD** — candidate public-domain music connector; verify current machine-readable catalog/download structure before adapter implementation.
- **Free Music Archive** — candidate open/creator music connector if a current stable API/data export is available; otherwise use metadata/link discovery only.
- **Internet Archive netlabels/live-music collections** — collection-level direct-download connectors using the generic Archive.org integration.

#### Connector format normalization for creative assets

Extend normalized file roles beyond conventional media formats:

- `master`
- `preview`
- `thumbnail`
- `source_project`
- `render`
- `model`
- `texture`
- `normal_map`
- `roughness_map`
- `displacement_map`
- `hdri`
- `audio_stem`
- `score`
- `midi`
- `caption`
- `transcript`
- `ocr`
- `supplement`
- `installer`
- `dlc_installer`
- `archive`
- `printable_model`

This allows Media Intake to represent a Blender open movie, game asset pack, museum scan, audiobook, ebook, and game installer through the same acquisition model without pretending all downloadable files are equivalent.


### Stock media, fonts, courseware, and repository hubs

#### Stock/open photography and video

- **Pexels API** — P1 searchable photo/video connector. The documented API exposes the Pexels content library, including photos and videos, with creator/source metadata and file variants. Respect the API's linking/credit requirements in UI.
- **Pixabay API** — P1 searchable image/video connector. Its REST API returns downloadable royalty-free images and videos plus creator/source metadata.
- **Unsplash API** — P1 image connector. Photo records expose API/HTML/download relations; implement the documented download-tracking/hotlink behavior rather than treating image CDN URLs as permanent canonical files.
- **Openverse** — retain as the preferred cross-provider open image/audio aggregation layer.
- **Flickr / Flickr Commons** — candidate photo connector through current API capabilities; store per-item license and originating collection.

#### Fonts / typography as downloadable creative media

Add a **Font** creative-media subtype so font families can be tracked like other versioned assets.

- **Google Fonts Developer API** — P0/P1 font metadata/download connector. The API returns family, category, subsets/scripts, variants, version, last-modified date, variable-font axes and direct font-file URLs for each variant.
- **Fontsource** — candidate open-source font package connector through npm/GitHub package metadata and releases.
- **Open Font Library / individual foundry GitHub releases** — candidate open-font sources.
- **Iconify** — candidate icon-set aggregator/protocol. A single adapter can normalize icons from many open icon collections and export SVG/JSON where current API/licensing permits.
- **Wikimedia Commons / Internet Archive** — fallback historical typography/type-specimen sources.

Font entity fields should include:
- family
- foundry/designer
- style/weight
- variable axes
- scripts/subsets
- version
- file format (TTF/OTF/WOFF/WOFF2)
- license
- upstream repository/source

#### Open courseware / lectures / educational collections

Add an optional **Course / Educational Media** family with course → module → lecture → resource relationships.

- **MIT OpenCourseWare** — P1 download-capable course connector. Courses expose downloadable course ZIP packages containing HTML and static resources such as PDFs, Word/LaTeX documents, code and data; many courses also expose individually downloadable lecture-video files.
- **OpenLearn / OpenStax / university OER repositories** — candidate open educational connectors when structured feeds/APIs/download links are available.
- **OER Commons / MERLOT** — candidate educational metadata aggregators that can resolve out to downloadable/open resources.
- **Internet Archive educational collections** — reuse the generic Archive connector.
- **YouTube/PeerTube lecture playlists** — video relationship/discovery layer, not generic direct-download unless the originating platform exposes it.

Normalize resource roles such as:
- syllabus
- lecture_notes
- lecture_video
- lecture_audio
- slides
- assignment
- solution
- reading
- textbook
- code
- dataset
- lab
- transcript

#### Repository/file hubs as generalized media connectors

- **Hugging Face Hub** — P1 generic repository/download connector for datasets, models, Spaces and arbitrary repository files. The official Hub API/client supports metadata lookup, single-file downloads, filtered snapshots, full-repository downloads, revisions/tags/commits and authenticated private repositories. This is useful for image/audio/video/text datasets and other media-adjacent collections.
- **GitHub Releases** — P1 generic release-asset connector for media projects, asset packs, datasets, fonts, books, mods and creator-distributed files. Preserve repository, release/tag/commit, release asset ID, checksum when available and license metadata.
- **GitLab Releases/Packages** — candidate equivalent connector.
- **Zenodo** — P1 scholarly/dataset/software/media repository connector via DOI/record APIs with downloadable files and checksums.
- **Figshare** — candidate research/media repository connector for downloadable datasets, figures, videos, posters and supplementary files.
- **OSF (Open Science Framework)** — candidate project/file connector for public or user-authorized research project files.
- **Kaggle** — candidate authenticated dataset connector for user-authorized/public datasets where API terms permit.

#### Public-domain/open film collections

- **Prelinger Archives** — treat as a specialized collection profile on top of the Internet Archive connector rather than building a duplicate downloader.
- **Internet Archive Feature Films / Community Video / animation collections** — collection presets on top of the same IA acquisition engine.
- **Library of Congress film/video collections** — use loc.gov/IIIF/file links when exposed.
- **NASA / government archival film collections** — source presets on the shared public-media connector.

#### Connector capability: collection presets

Add provider-level **collection presets** so one connector can expose curated sub-sources without separate code.

Examples:

- Internet Archive → Prelinger Archives
- Internet Archive → Live Music Archive
- Internet Archive → Netlabels
- Internet Archive → Software Library
- Internet Archive → Feature Films
- Wikimedia Commons → NASA media category
- Hugging Face → audio datasets
- Hugging Face → image datasets
- MIT OCW → lecture-video courses

A preset should define:
- provider
- query/filter
- media subtype
- expected file roles/formats
- default refresh cadence
- default provenance/attribution fields
- optional local storage policy

This keeps the codebase small while still presenting many specialized connectors in the UI.


---

# Phase 4 — Universal automated acquisition, indexers, torrents, Usenet, and dependency-optional downloading

**Priority: P0 / High priority**

This phase turns Media Intake from a watched-media/discovery application into a **media-agnostic automated acquisition and library-reconciliation system**. The guiding rule is that Media Intake owns the durable intent, state, provenance, history, scoring, and policy. External tools such as Prowlarr, Sonarr, Radarr, Lidarr, qBittorrent, SABnzbd, NZBGet, aria2, Transmission, Deluge, Suwayomi, Pinchflat, Tube Archivist, Chaptarr, Mylar/Kapowarr, or similar systems may be used as optional execution backends, but none should be required for Media Intake to retain a complete understanding of what the user wants, what is missing, what candidates were found, why one was selected, how it was acquired, and what remains to do.

The default architectural goal is:

- **zero mandatory external applications**
- **standard-library-first Python core where practical**
- external/bundled engines supported through stable APIs when available
- native protocol implementations added incrementally behind the same interfaces
- all provider-specific behavior hidden behind provider-neutral contracts
- broad search/discovery separated from actual acquisition authorization
- all automatic acquisition routed through existing rights/provenance policy checks

## Core acquisition pipeline

Normalize every automatic acquisition into this pipeline:

1. watched intent / library gap / upgrade opportunity is identified
2. generate structured search request and alternate query forms
3. search all appropriate enabled sources
4. normalize results into release candidates
5. merge duplicate observations of the same release
6. score candidates
7. apply rights, policy, safety, quota, and quality gates
8. choose a candidate
9. choose an acquisition engine
10. submit/grab the release
11. monitor transfer state
12. validate downloaded content
13. quarantine unsafe/uncertain content
14. extract/repair/post-process when required
15. import into the destination library
16. reconcile the resulting asset against the logical media graph
17. continue watching for missing items, future releases, and upgrades

Provider-independent interfaces should include concepts similar to:

- `media.search(request)`
- `media.search.providers(request)`
- `media.release.normalize(raw_result)`
- `media.release.dedupe(candidate)`
- `media.release.score(candidate, profile)`
- `media.acquire.plan(entity_id)`
- `media.acquire.submit(candidate_id)`
- `media.acquire.status(job_id)`
- `media.acquire.retry(job_id)`
- `media.acquire.cancel(job_id)`
- `media.library.reconcile(entity_id)`

## Universal watch intent

Replace media-specific acquisition assumptions with a generic watch intent.

Suggested fields:

- entity_id
- media_kind
- monitor_mode
- acquisition_policy
- quality_profile_id
- language_profile_id
- release_policy_id
- search_policy_id
- retention_policy_id
- preferred_acquisition_backend
- allowed_protocols
- provider_scope
- parent_policy_id

Suggested monitor modes:

- none
- metadata_only
- future_only
- new_releases
- missing
- all
- selected
- upgrade_until_cutoff

Policy inheritance should continue through:

1. global defaults
2. media-family defaults
3. subtype defaults
4. franchise
5. series
6. season/arc/collection
7. individual work/release

## Search Broker

Introduce a central `SearchBroker` that fans out one structured search request to many source adapters and returns normalized candidates.

The initial source protocol set should include:

- Torznab
- Newznab
- RSS
- Atom
- OPDS
- JSON APIs
- generic HTTP APIs
- configurable HTML/site definitions
- existing Media Intake native catalog/download adapters
- optional external search aggregators

Do not require Prowlarr/Jackett/NZBHydra for core operation, but support them as optional adapters because they provide useful ecosystem compatibility and broad site coverage.

### Generic standardized indexers

Implement first-class native support for:

- generic Torznab endpoints
- generic Newznab endpoints
- capability discovery
- API key authentication
- category mapping
- paging
- recent/release feeds
- ID-aware searches where supported
- media-specific search functions where supported
- per-indexer rate limits
- health probes
- response statistics

Adding a standard indexer should normally require only:

- URL
- API key or credentials where required
- optional enabled categories
- optional rate/quota settings

### Automatic source probing

When a source URL is added, try to identify its capabilities automatically:

1. Newznab/Torznab CAPS probe
2. RSS/Atom feed discovery
3. OPDS discovery
4. JSON/OpenSearch/API hints
5. generic configurable source-definition path

Store the result as source capabilities rather than baking assumptions into UI code.

## Declarative site-definition engine

Build a Cardigann-like **Media Source Definition** engine so new searchable/downloadable websites can be added primarily through data definitions rather than Python code.

Prefer JSON as the built-in definition format to avoid requiring YAML parsing dependencies.

Definitions should support:

- source ID/name
- base URLs/mirrors
- source protocol
- categories
- authentication mode
- cookies
- headers
- API keys
- login forms where appropriate
- GET/POST search actions
- pagination
- query templates
- result selectors/parsers
- HTML/XML/JSON result parsing
- release fields
- torrent/magnet/NZB/direct-download links
- source-specific rate limits
- source health checks
- configurable timeout/retry behavior
- capability flags

Maintain definitions separately from the core application so source integrations can be updated without a full application release.

Do not blindly copy GPL source code or third-party definitions without a license review. Prefer compatible protocol implementations, independently authored definitions, or explicit integration with existing services where licensing makes direct reuse inappropriate.

## Normalized release candidate

Every source result should normalize into a common `ReleaseCandidate` model.

Suggested fields:

- candidate_id
- logical_entity_id
- title
- normalized_release_name
- source_id
- source_type
- source_result_id
- source_url
- protocol
- download_uri / magnet / NZB locator
- info_hash where applicable
- published_at
- size
- language
- media format
- quality
- resolution
- codec
- audio codec
- channels
- HDR/DV flags
- edition
- release group
- version/revision
- seeders
- leechers
- Usenet age
- provider completion/availability
- trusted/verified flags
- source metadata
- provenance
- initial match confidence
- score explanation
- rejection reasons

The candidate object should be independent from the downloader used to retrieve it.

## Candidate deduplication

The same underlying release may appear through several indexers. Merge provider observations rather than presenting them as independent releases.

Preferred release identities:

### Torrent
- v1/v2 info hash
- normalized torrent metadata fingerprint

### Usenet
- NZB content fingerprint
- message-ID set fingerprint
- normalized release name + size where no stronger identifier is available

### Direct/file sources
- SHA-256 where known
- canonical file URL
- source asset ID
- logical edition/release identity

A normalized release may therefore have multiple availability routes:

- provider A
- provider B
- provider C

If one provider fails, the acquisition planner can try another route without rediscovering the release.

## Release scoring and quality profiles

Move beyond fixed source priority and format priority.

Use data-driven quality profiles and custom rules.

Possible scoring dimensions:

- entity/title match confidence
- exact external-ID match
- quality target
- preferred format
- language
- edition
- release group
- source trust
- provider historical success
- availability/completion
- torrent health
- Usenet retention/age
- file size expectations
- wanted/unwanted traits
- codec preferences
- source latency
- quota/cost considerations
- prior failure history
- suspicious-match penalties

Media-specific dimensions should remain profile data.

Examples:

### Video
- resolution
- source type
- HDR/DV
- codec
- audio codec/channels
- release group
- language/subtitles

### Books
- EPUB/AZW3/PDF preference
- ISBN/edition match
- publisher
- language

### Audiobooks
- M4B/other format
- narrator
- unabridged status
- bitrate
- chapters
- edition

### Music
- FLAC/lossless
- bit depth
- sample rate
- edition
- MusicBrainz match

### Comics/manga
- CBZ/CBR/PDF
- language
- completeness
- volume/issue/chapter mapping
- image quality

Expose score components and rejection reasons in manual search UI so users can understand why a candidate was accepted or rejected.

## Adaptive provider ranking

Track source performance and learn which providers work best for each media family/query type.

Metrics should include:

- response latency
- availability rate
- search success rate
- acquisition success rate
- false-match rate
- timeout/error rate
- torrent seed availability
- Usenet completion
- average observed speed where measurable
- API throttling
- quota exhaustion
- provider uptime
- duplicate-result rate
- metadata quality
- historical success by media type

Use these statistics to bias provider order and reduce unnecessary requests.

A useful long-term objective is maximizing:

`expected successful acquisition / request cost`

without allowing adaptive ranking to hide all results from lower-ranked sources.

## Search query generation

Generate multiple structured search variants from the media graph.

Inputs may include:

- canonical title
- aliases
- original-language title
- year
- creator/author
- series name
- season/episode
- volume/chapter
- edition
- ISBN
- IMDb
- TMDB
- TVDB
- MusicBrainz
- AniList
- provider IDs

Learn which query forms are most effective per provider while preserving deterministic fallback behavior.

## Separate new-release monitoring, backlog hunting, and upgrades

Do not treat every search as the same type of job.

Create three independent scheduler classes:

### RecentReleaseScanner
Processes recent provider feeds/events and asks:
“Does this newly observed release satisfy anything currently watched?”

### BacklogHunter
Periodically searches older missing items using pacing, cooldowns, and provider budgets.

### UpgradeHunter
Searches assets that exist but remain below their configured quality cutoff.

Each scheduler should support:

- rate budgets
- per-provider cooldowns
- per-entity cooldowns
- maximum searches per period
- priority queues
- manual pause/resume
- retry backoff
- recent-failure suppression

This prevents constant brute-force searching while ensuring old missing items do not remain forgotten forever.

## Event-driven release ingestion

Add a common release-event endpoint so Media Intake can react immediately to new release signals.

Possible event sources:

- RSS
- Atom
- IRC release announcements
- webhooks
- Torznab/Newznab feeds
- Prowlarr
- autobrr-like event sources
- filesystem watchers
- provider APIs
- external manager notifications

Normalize all events into:

`ReleaseEvent -> candidate normalization -> watched-intent matching -> scoring -> policy check -> acquisition`

The event origin should not change the rest of the pipeline.

## Acquisition engine abstraction

Define a generic engine interface such as:

- submit
- status
- pause
- resume
- cancel/remove
- list_files
- set_file_priority
- health
- get_transfer_stats

Media Intake should support multiple engines for the same protocol.

### Torrent engines

Target adapters:

- NativeTorrentEngine
- qBittorrent
- Transmission
- aria2
- Deluge
- future compatible torrent clients

External adapters should use their HTTP/RPC APIs through the Python standard library where practical so no client-specific Python package is required.

### Usenet engines

Target adapters:

- NativeUsenetEngine
- SABnzbd
- NZBGet
- future compatible NZB downloaders

### Direct/specialized engines

Target adapters:

- direct HTTP/HTTPS
- aria2
- pyLoad-compatible APIs
- yt-dlp
- gallery-dl
- FanFicFare
- Suwayomi
- source-specific direct file adapters

Specialized tools remain optional.

## Native BitTorrent engine roadmap

A fully native BitTorrent engine is desirable for dependency independence, but should be implemented in stages behind the same engine interface.

### Stage 1
- bencode encode/decode
- .torrent parsing
- v1 info hash
- HTTP trackers
- TCP peer handshake
- bitfield/have handling
- piece requests
- SHA-1 verification
- single/multi-file torrents
- selective file download
- resumable piece state
- seeding basics

### Stage 2
- magnet URI parsing
- BitTorrent extension protocol
- metadata exchange
- reconstruct metainfo from peers

### Stage 3
- UDP trackers
- DHT/Kademlia routing
- bootstrap nodes
- get_peers
- announce_peer
- peer cache

### Stage 4
- PEX
- multi-tracker behavior
- IPv6
- web seeds
- local peer discovery
- uTP
- NAT traversal where practical
- protocol encryption where appropriate
- BitTorrent v2/hybrid torrent support
- queue/seeding policies

The native engine should not block support for mature external engines; both use the same abstraction.

## Native Usenet engine roadmap

Implement a standard-library-first NNTP/NZB engine incrementally.

### Core
- NNTP TCP connection
- TLS
- authentication
- capability negotiation
- multiple servers
- server priority
- fallback servers
- connection pools
- retries/timeouts
- quotas
- article retrieval

### NZB processing
- XML NZB parser
- segment ordering
- article scheduling
- multi-server failover
- yEnc decoding
- CRC verification
- file assembly
- resumable state

### Repair/extraction
Native support should initially include straightforward archive handling available through standard Python libraries. Advanced PAR2 repair and RAR/7z extraction may use optional external tools until robust native implementations are available.

Optional helpers may include:

- par2cmdline
- 7-Zip
- unrar-compatible tools

A future native PAR2 implementation may be researched separately if eliminating all external repair dependencies is worth the engineering cost.

## Provider/source registry

Create a single provider registry rather than separate hard-coded lists for torrents, Usenet, feeds, and direct sites.

A source should declare capabilities such as:

- search
- recent releases
- categories
- movie search
- TV search
- book search
- music search
- comic/manga search
- magnet
- torrent file
- NZB
- direct file
- stream
- authentication type
- quota availability

Persist:

- provider ID
- protocol
- base URLs/mirrors
- capabilities
- category mapping
- authentication configuration reference
- rate limits
- quotas
- last health result
- last successful request
- rolling latency
- rolling error rate
- rolling success rate
- last throttle/ban
- media-family success statistics

## Canonical media taxonomy

Do not make tracker/indexer category IDs canonical.

Use Media Intake categories such as:

- media.video.movie
- media.video.tv
- media.video.anime
- media.audio.music
- media.audio.audiobook
- media.text.book
- media.text.lightnovel
- media.comic.comic
- media.comic.manga
- media.game
- media.asset.*
- media.education.*
- other future types

Each provider adapter maps the canonical category to provider-specific categories.

## Acquisition job state machine

Replace simplistic queued/downloading/downloaded state assumptions with a durable state machine.

Suggested states:

- discovered
- wanted
- search_queued
- searching
- candidate_selected
- grabbed
- queued
- downloading
- downloaded
- verifying
- repairing
- extracting
- postprocessing
- importing
- reconciling
- complete
- retry_scheduled
- blocked
- needs_review
- failed
- cancelled

Persist every meaningful transition in a history table.

A failed candidate should be able to trigger:

1. record failure
2. block/penalize candidate or release when appropriate
3. select next candidate
4. retry through another provider or engine
5. stop only after policy-defined exhaustion

## Quarantine and verification

Broad automated source support requires a strict quarantine stage.

Suggested layout:

- incoming/quarantine
- incoming/verified
- incoming/rejected

Verify as appropriate:

- file type and magic bytes
- expected media family
- archive integrity
- torrent hashes
- CRC/checksum
- size plausibility
- path traversal
- symlink escape
- unexpected executables
- metadata/entity match
- expected season/episode/volume/chapter
- duplicate assets

Never automatically execute acquired binaries.

Executable/software/game acquisitions should have stricter policies and explicit trust/authorization rules.

## Post-processing pipeline

Create provider-independent post-processing steps.

Possible stages:

- archive extraction
- PAR2 verification/repair
- filename normalization
- container inspection
- checksum
- metadata identification
- tagging
- media conversion where user-configured
- subtitle/caption matching
- chapter/track splitting or joining
- cover/artwork handling
- Calibre import
- beets/music tagging
- FFmpeg processing
- library move/link/copy
- deduplication

Model post-processors as optional capabilities so a missing optional tool does not break unrelated media types.

## External manager adapters

Media Intake should be able to delegate acquisition execution to mature media managers without surrendering canonical state.

Target adapters:

- Sonarr
- Radarr
- Lidarr
- Chaptarr
- Mylar3
- Kapowarr
- Suwayomi
- Pinchflat
- Tube Archivist
- ytdl-sub
- Audiobookshelf where relevant

Example:

1. Media Intake resolves and stores the logical entity.
2. Policy chooses Sonarr as the current TV acquisition backend.
3. Media Intake creates/updates the monitored series in Sonarr.
4. Sonarr performs its normal acquisition.
5. Media Intake receives/polls completion.
6. Media Intake reconciles the imported file into its own asset/provenance history.

Changing the acquisition backend later must not alter the canonical watched entity.

## Library assets and versions

Do not assume one work equals one file.

Represent:

- logical entity
- edition/release
- acquired asset
- asset version
- physical file
- source/provenance
- quality
- language
- acquisition history

One logical work may legitimately have multiple assets, such as:

- ebook
- audiobook
- illustrated edition
- alternate narrator
- subtitle variants
- remaster/remake-related assets
- source project + rendered asset

Track quality cutoffs independently per asset role.

## Database additions

Add tables/concepts similar to:

- watch_policies
- quality_profiles
- custom_format_rules
- source_instances
- source_capabilities
- source_health
- source_quotas
- source_statistics
- source_category_mappings
- search_jobs
- search_attempts
- release_candidates
- release_observations
- candidate_scores
- candidate_rejections
- release_identities
- blocked_releases
- acquisition_jobs
- acquisition_history
- download_jobs
- download_files
- postprocess_jobs
- import_jobs
- library_assets
- asset_versions
- scheduler_jobs
- scheduler_budgets
- provider_query_performance

Keep migration paths from current `items` and `watched_items`.

## UI requirements

Add universal views:

### Wanted
- Missing
- Upcoming
- Searching
- Unavailable
- Waiting for retry

### Activity
- Queued
- Downloading
- Verifying
- Repairing
- Processing
- Importing
- Failed
- Blocked
- Needs Review

### Upgrades
Show:
- current asset quality
- target profile
- cutoff status
- better candidate availability
- score improvement
- reason an upgrade is or is not wanted

### Manual Search
Show all normalized candidates with:
- score
- provider(s)
- protocol
- quality
- size
- availability
- score explanation
- rejection reasons
- manual grab override where permitted

### Sources
Show:
- health
- protocol
- supported categories
- recent success rate
- latency
- quota/rate status
- last error
- last successful request
- enable/disable
- test button

### Download Engines
Show:
- active engine
- queue
- health
- transfer stats
- external/native backend
- optional dependency status

## Security and credentials

- Keep provider credentials outside Git.
- Use environment variables or an OS/keychain/secret-store abstraction where possible.
- Never expose credentials in logs.
- Treat source URLs, cookies, API keys, NNTP credentials, and remote downloader credentials as secrets.
- Support per-source TLS verification and explicit opt-in for exceptional cases; do not disable TLS validation globally.
- Respect rate limits and provider terms.
- Keep broad discovery separate from authorization to acquire a specific work/source.
- Preserve rights/source provenance in all acquisition decisions.

## Implementation order

Recommended sequence:

### Phase 4A — acquisition data model
- normalized SearchRequest
- ReleaseCandidate
- quality profiles
- acquisition job state machine
- source registry
- library asset/version model
- migrations

### Phase 4B — standardized search
- native Newznab
- native Torznab
- RSS/Atom
- provider health
- category mapping
- SearchBroker
- candidate normalization/deduplication

### Phase 4C — scoring and scheduler split
- release scoring
- custom rules
- RecentReleaseScanner
- BacklogHunter
- UpgradeHunter
- cooldowns/budgets
- manual-search explanations

### Phase 4D — download engine abstraction
- existing direct HTTP wrapped as engine
- qBittorrent adapter
- Transmission adapter
- aria2 adapter
- SABnzbd adapter
- NZBGet adapter
- engine health/status UI

### Phase 4E — external manager adapters
- Sonarr
- Radarr
- Lidarr
- Chaptarr
- comic/manga/video-specialized managers

### Phase 4F — declarative site definitions
- JSON schema
- generic HTML/XML/JSON interpreter
- definition validation
- source-definition test harness
- hot-reload/update mechanism

### Phase 4G — native torrent engine
Implement staged BitTorrent functionality behind the existing TorrentEngine interface.

### Phase 4H — native Usenet engine
Implement staged NNTP/NZB/yEnc functionality behind the existing UsenetEngine interface.

### Phase 4I — advanced post-processing and repair
- extraction
- repair
- tagging
- conversion
- stronger validation
- failed-download retry/blocking

### Phase 4J — adaptive optimization
- provider-performance learning
- query-strategy learning
- score calibration
- automatic routing by expected success/request cost
- search-efficiency dashboards

## Acceptance criteria

This phase is successful when:

- Media Intake can search multiple Torznab/Newznab sources without Prowlarr.
- Prowlarr/Jackett/NZBHydra can still be added as optional compatible sources.
- Media Intake can use at least one torrent downloader and one Usenet downloader through provider-neutral engine APIs.
- A source result is normalized independently from the downloader that later retrieves it.
- Duplicate release observations from multiple providers merge into one logical release.
- Candidate ranking explains why a release was accepted or rejected.
- missing, new-release, and upgrade searches operate as separate scheduler concerns.
- failed acquisitions can automatically fall back to another candidate/provider according to policy.
- all acquired content passes through quarantine/verification before final import.
- Media Intake's own database remains canonical if every external manager/downloader is disconnected.
- users can choose external mature engines or native engines without changing their watched-media state.
- native torrent and Usenet implementations can be improved incrementally without changing higher-level orchestration.
- no source credentials or secrets are committed to Git.
- existing rights/provenance gates continue to control whether a discovered candidate may actually be acquired.
