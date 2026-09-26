# Media Intake Roadmap

This roadmap extends the existing Media Intake watched-media system from title/series-level monitoring into a general **franchise, continuity, and release graph** that can automatically discover related media with minimal manual input.

The implementation order is intentional:

1. **Franchise / continuity auto-expansion**
2. **Game watching**
3. **Movie watching**
4. **TV watching**
5. **Web-series watching**

Later media types should reuse the same entity, relationship, provenance, refresh, confidence, and review infrastructure rather than creating independent one-off watch systems.

---

## Guiding principles

### Minimal-input intake

The normal user flow should be:

1. Enter or select one title.
2. Media Intake resolves the entity and media type.
3. The app discovers its series/franchise/continuity relationships.
4. High-confidence direct continuations and installments are added according to the user's watch policy.
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

# Phase 3 — Movie watching

**Priority: P2**

## Goal

Track individual films and film franchises, including future sequels and related branches.

## Candidate provider adapters

- TMDB
- Wikidata
- official studio/distributor sources for authoritative announcements

## Entity coverage

Support:

- film
- film collection/franchise
- sequel
- prequel
- spin-off
- reboot
- remake
- alternate cut/edition
- direct-to-video/streaming film
- announced/untitled project

## Watch events

Detect:

- sequel/prequel/spin-off announcement
- title change
- release date
- delay
- production-status change when reliable
- theatrical release
- streaming/home-media availability when enabled
- newly linked franchise/collection entry

A reboot/remake should be related to the prior continuity without being silently inserted into the same chronological sequence.

## Acceptance criteria

- Adding a movie can discover its collection/franchise.
- Future sequels are added/discovered according to policy.
- Remakes/reboots remain separate continuity branches.
- Release changes update existing records rather than create duplicates.

---

# Phase 4 — TV watching

**Priority: P3**

## Goal

Track television series, seasons, episodes, specials, spin-offs, sequel shows, and shared television universes.

## Candidate provider adapters

- TMDB
- TVmaze
- Wikidata
- official network/streamer pages when available

## Entity coverage

Support:

- television franchise/universe
- show/series
- season
- episode
- special
- miniseries
- sequel series
- prequel series
- spin-off
- revival
- reboot

## Watch granularity

Allow policies at multiple levels:

- franchise
- show
- season
- episode

Examples:

- track only new seasons
- track every episode
- track sequel/spin-off show announcements
- track specials but not talk/recap episodes

## Watch events

Detect:

- renewal
- cancellation/end
- new season
- season premiere date
- episode schedule
- special
- sequel/prequel/spin-off show
- revival/reboot
- schedule/date changes

## Acceptance criteria

- Adding a show can discover seasons and related shows.
- New seasons appear automatically.
- Episodes can be watched without forcing episode-level tracking globally.
- Spin-offs/revivals/reboots are classified correctly.
- Provider schedule changes update the same release record.

---

# Phase 5 — Web-series watching

**Priority: P4**

This phase should build on the app's existing RSS/Atom web-serial and fanfiction capabilities rather than creating a parallel subsystem.

## Goal

Watch serialized video/audio/mixed-media productions published primarily on the web, including series that lack traditional TV metadata.

Examples may include:

- YouTube serialized shows
- creator-hosted web series
- RSS-backed episodic productions
- public podcast/video feeds where episodes form a narrative series
- independent animation/web productions

## Discovery layers

Prefer structured sources first:

- RSS/Atom
- YouTube channel/playlist metadata where available
- Wikidata
- creator/site metadata
- supported page adapters

Use site-specific scraping only where permitted and necessary.

## Entity coverage

Support:

- web series
- season/arc
- episode
- special
- playlist
- sequel series
- spin-off
- creator channel as a related entity

Do not equate every creator upload with an episode.

## Episode identification

Use combinations of:

- playlist membership
- series metadata
- title patterns
- descriptions
- publication order
- explicit season/episode numbering
- source-provided relationships

Ambiguous uploads should be candidates rather than automatically injected into the canonical episode list.

## Watch events

Detect:

- new episode
- new season/arc
- finale/completion
- hiatus/return where reliable
- sequel/spin-off series
- feed/playlist move
- title/number correction

## Acceptance criteria

- A structured web series can baseline existing episodes and watch only new ones.
- Playlists/feeds can become series membership sources.
- Unrelated creator uploads are not automatically classified as episodes.
- Sequel/spin-off web series can participate in the same continuity graph as books/games/film/TV when evidence exists.

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

- database migration
- media entity/external-ID tables
- normalized relationship model
- relationship evidence/provenance
- graph diff
- review queue primitives
- migration path from existing watches

## Milestone 2 — Book franchise proof of concept

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

## Milestone 3 — Unified GUI workflow

- Add Anything
- automatic media-type/provider routing
- import summary
- continuity/related panel
- watch-policy editor
- Needs Review

## Milestone 4 — Games

- IGDB/Wikidata adapters
- game-specific relationship normalization
- release/DLC monitoring

## Milestone 5 — Movies

- TMDB/Wikidata adapters
- collections/franchises
- sequel/reboot/remake handling

## Milestone 6 — TV

- TMDB/TVmaze/Wikidata
- seasons/episodes/specials
- renewal/schedule monitoring

## Milestone 7 — Web series

- extend existing feed/web-serial infrastructure
- playlist/feed series membership
- web-series continuity relationships

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
10. apply the same behavior consistently across books, games, movies, TV, and web series.
