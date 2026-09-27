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
