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

When the user selects a series from Search and add, keep one parent Watched item
for the series and create one child watch/queue request for each unique logical
book in the series. Show member books with series names and positions in series
order. Search connected download sources for each child work; editions, split
volumes, and collections must not create duplicate child books.

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

All catalog HTTP requests share a transport helper that closes HTTP error response
streams while re-raising the original status for provider-specific fallback and review
handling. A regression test covers this cleanup path.

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
- [x] Provider-neutral, graph-only refresh for Wikidata, AniList, TMDB, TVMaze, IGDB, Steam, Goodreads, Google Books, Open Library, and RanobeDB catalog IDs
- [x] Record Goodreads and Open Library author bibliography entries as explicit work-to-creator graph edges
- [x] Manually refresh Goodreads and Open Library author bibliographies into the graph without creating watches or download searches
- [x] Refresh provider relationships across the bounded continuity neighborhood without creating watches or download searches
- [x] Continuity window action to refresh linked catalog relationships and show partial-provider failures

Wikidata discovery stores linked entities and source claims in the graph. Search and
add keeps one parent series watch while creating a child book watch and queue request
for each unique direct member. The series title and member relationships remain in the
graph; each released child is searched through its connected book sources.

## Milestone 2 — Book franchise proof of concept

Status: In progress. Books search federates Goodreads, Open Library, Google Books,
Wikidata, NovelArchive, LNORI, and RanobeDB. Series results merge provider memberships and
sequence evidence, create one parent watch, and queue every unique member book for
automatic per-work source searches. Explicit Wikidata parent-series and sequel links
can expand standalone book watches. Broader validation of related sequel-series
matches and non-equivalent title variants remains open; checksummed ISBNs, inverted
articles, punctuation, and common format labels support conservative work matching.
Uncertain title/creator conflicts remain in Needs Review. Provider disagreements for
dates, lifecycle status, and series positions remain in Needs Review and have explicit
canonical-choice actions that apply to the watched record and preserve the competing
observations.

- [x] Refresh Open Library series-member relationships with explicit catalog IDs and sequence positions
- [x] Search Google Books as a metadata-only catalog and retain explicit series positions, volume IDs, ISBNs, and publication dates
- [x] Refresh explicit Google Books series membership on scheduled scans and automatically queue newly listed works once
- [x] Collapse Books search rows with a shared validated ISBN and compatible author while retaining each catalog record; keep author conflicts separate
- [x] Give direct prequel works and direct prequel series separate configurable auto-add policies
- [x] Refresh RanobeDB volume memberships and English release observations in the graph without creating download watches
- [x] Merge provider records with a checksum-valid shared ISBN-10/13 and retain both catalog identities and title evidence
- [x] Reuse shared checksum-valid ISBN identities during graph refresh only when book title and known author agree; send conflicts to Needs Review
- [x] Collapse explicit common-format title suffixes without a shared ISBN when the author matches; retain each catalog record as provenance
- [x] Normalize catalog-inverted English articles (for example, “Hobbit, The”) for title matching while preserving edition labels
- [x] Reuse the normalized Books work-title key when linking possible graph duplicates; keep matches in Needs Review
- [x] Route different titles with the same known author and series position to Needs Review instead of auto-queueing both candidates
- [x] Resolve new catalog IDs through existing child watches before syncing series edges, preventing a second graph entity for a matched format variant
- [x] Let Needs Review choose a canonical book release date, lifecycle status, or series position and apply it to the graph and watched record
- [x] Limit the Books Wikidata series fallback to descriptions that identify books, novels, novellas, or short-story collections instead of returning arbitrary TV/video series
- [x] Keep explicit non-book Wikidata series members out of book watches; hold unclassified members for review and retain their type evidence

Adding a standalone Wikidata-linked book now follows its explicit Wikidata parent
series, creates the series watch, attaches the original book, and imports other
numbered or dated members once. A directly linked sequel or prequel series is also
expanded immediately so its member books enter the watch and search queues without
waiting for the next scheduled refresh. Traversal follows up to three linked book
series and stops when it encounters a series already in the current path. Stable
catalog IDs are used to avoid duplicate graph nodes when a member already appears in
another catalog. Refreshes still depend on provider availability and series
membership quality; deprecated Wikidata statements are excluded from type, sequel,
series-membership, and release-date resolution, with claim rank retained in graph
evidence. Broader sequel-series validation remains open.
When multiple book catalogs describe the same series member, the merged row keeps
each provider's title, author, release date, series position, source URL, and catalog
ID. Those variants and all catalog IDs are retained on the child watch and linked into
the graph; release-date and series-position disagreements appear in Needs Review.
Cross-provider release-date and lifecycle-status disagreements on graph entities also
enter Needs Review with the competing values, providers, source URLs, and observation
times; refreshing an unchanged conflict does not duplicate the review item. The review
window can select a canonical date, status, or series position. Date choices update the
watch schedule and visible queue request; position choices update the child watch and
series edge and persist through catalog refreshes. Completed downloads and manually set
release dates are preserved.
Goodreads, Open Library, and Google Books ISBN values are normalized and checksum-validated; ISBN-10
and its equivalent ISBN-13 resolve to one logical series member and child watch even
when the catalog titles differ. Both catalogs' IDs, titles, and metadata stay attached
as provenance. Search results use the same ISBN identity rule while keeping known
author conflicts separate. Catalog variants without a shared ISBN or stable work
identifier collapse when the author is compatible and the only title difference is a
final explicit common format label (for example, “Paperback” or “Kindle Edition”);
all source titles and IDs remain in the provenance variants. Less predictable title
changes still rely on exact title/author matching or enter the existing packaging
review path. When two different titles have the same known author and explicit series
position, the second stays out of the automatic queue and is shown in Needs Review
with the first catalog record attached for comparison. Accepting the candidate adds it
as a separate book; rejecting it leaves the original member in place. Labels such as
“Revised Edition” are preserved because they can describe meaningful content changes.
During Goodreads series refresh, child-watch matching now runs before graph membership
sync; the new catalog ID is linked to the existing work first, and the catalog's
membership evidence is added to its current graph edge without creating a second
entity or an inverse duplicate edge.
Google Books remains metadata-only in both Search and add and Settings priority; the
catalog's year-only publication dates are retained as evidence without being turned
into exact release dates. Its series expands automatically only when multiple returned
records explicitly share a named series reference.

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

The member list retains provider IDs and position evidence. Explicit provider
numbers take precedence; publication-date placement for unnumbered novellas is
marked as an estimate. Collection and split-edition hits are filtered when a
provider supplies the series' direct member list. Wikidata-linked members with
neither an explicit series position nor a publication date are held out of the
automatic queue and saved as uncertain candidates in the local review queue.
Provider gaps can be filled by evidence-backed continuity supplements. For
Stormlight this adds the released Elsecaller / King Lopen double book and the
announced Horneater novella at 4.5. Horneater's Goodreads 2026 estimate is
overridden by the author's newer unscheduled status; its watched entry waits
for a confirmed or manually entered release date before searching download
sources. The series plan records that Book Six is planned but untitled and
undated, while the author describes a ten-novel plan overall.

Series rows whose titles look like split packaging (for example, a series title
followed by “Part One”), volume labels, or common format suffixes such as paperback
or Kindle edition stay out of the automatic book queue and appear in Needs Review.
Accepting that review creates the child watch and queue request; resolving the same
provider row later does not reopen the decision. A book whose title exactly matches
its series name is still allowed, since many series use that convention.

## Milestone 3 — Unified GUI workflow

Status: Core workflow implemented. Search and add supports category-scoped searches
and Auto-detect across Books, Comics, Manga, Anime, Videos, Games, Light novels,
Web serials, and Fanfiction URLs. Import preview, continuity view, relationship
review, and per-media watch policies are available. Remaining work is improving
cross-provider classification and provider coverage.

- [x] Category-based Add Anything search routing
- [x] Route GUI category and Auto-detect searches through the provider-neutral resolver entry point
- [x] Add Anything automatic media-type/provider routing
- [x] Import summary
- [x] Continuity / related panel
- [x] Watch-policy editor
- [x] Needs Review

## Milestone 4 — Games

Status: Core watch path implemented; additional publisher-specific store/API coverage remains open.

- [x] IGDB, Steam, and Wikidata metadata adapters
- [x] Game-specific relationship normalization
- [x] IGDB release, DLC, expansion, remake, remaster, and port tracking
- [x] Preserve IGDB release dates separately by platform and region
- [x] Compare matching IGDB and Steam region/platform releases and queue disagreements with evidence
- [x] Let review select a canonical platform/region observation and schedule the watched game's release-day check
- [x] Steam release and policy-controlled DLC tracking
- [x] Schedule Steam searches only on exact release days; preserve month/year text without inventing a day
- [x] Normalize IGDB remake, remaster, and port edges with the correct original-to-variant direction
- [x] Retain raw IGDB relationship fields and TMDB collection membership fields in relationship evidence
- [x] User-configured publisher/developer RSS/Atom feeds baseline existing posts and send new announcements to Needs Review
- [x] Extract typed renewal, cancellation, hiatus, premiere, availability, and date-change candidates from announcement titles, categories, and summaries; preserve text/date provenance and acceptance stores graph events
- [x] Preserve explicitly announced date precision; accepted exact game launch dates schedule the watch
- [x] Native structured release schedules from IGDB and Steam store APIs, with platform/region observations and exact-day scheduling rules

## Milestone 5 — Video foundation

Status: Shared Video watch path implemented; provider breadth and continuity coverage
remain open.

- [x] Unified Video media family and subtype model
- [x] Shared TMDB, TVMaze, AniList, and Wikidata normalization
- [x] Cross-format continuity relationships when catalog evidence is available
- [x] Common Video release/status watcher and change history
- [x] User-configured studio/network/streamer RSS/Atom announcement feeds with provenance and review candidates
- [x] Accept configured structured JSON media-schedule feeds with typed events, date, source URL, location, and native IDs
- [x] Read structured iCalendar event feeds; preserve event IDs, summaries, exact start dates, and review future schedule entries even on the first scan
- [x] Accepting a calendar episode candidate creates a child episode watch and queues its release-day search
- [x] Use iCalendar event start dates, not calendar update timestamps, for episode release scheduling
- [x] Extract typed renewal, cancellation, hiatus, premiere, availability, and date-change candidates from announcement titles, categories, and summaries; preserve text/date provenance and acceptance stores graph events
- [x] Preserve explicit premiere and availability date precision; accepted exact premiere dates schedule the watch
- [x] Configured first-party JSON, Schema.org JSON-LD Event/ItemList, and iCalendar schedules with typed premiere, renewal, availability, and episode dates

## Milestone 6 — Movie subtype

Status: Partial. TMDB collections and Wikidata continuations are represented; release
windows and distinct remake/reboot classification need broader source coverage.

- [x] TMDB collection/franchise links
- [x] Wikidata sequel/prequel discovery and policy handling
- [x] Preserve explicit Wikidata derivative-work links in the graph without auto-adding variants
- [x] Normalize explicitly described Wikidata spin-offs, remakes, reboots, revivals, alternate versions, and alternate cuts with variant-to-original edge direction; keep variants metadata-only and apply the selected spin-off watch policy
- [x] TMDB movie premiere, theatrical, digital, physical, and television windows by country
- [x] Normalize explicit spin-off, remake, and alternate-continuity edges from Wikidata and AniList; normalize IGDB remake edges
- [ ] Normalize reboot and alternate-cut edges from additional provider relationship fields when those catalogs expose them
- [x] Compare conflicting premiere, theatrical, digital, physical, and television windows by region and review source disagreements
- [x] Track review-approved streaming dates by named platform from configured studio/streamer feeds

Provider-field inventory for the remaining reboot/alternate-cut edge: the integrated
movie paths currently expose TMDB collection membership, AniList `REMAKE` and
`ALTERNATIVE` relations, and Wikidata derivative-work claims with explicit description
classification. None of those current response paths supplies another typed reboot
or alternate-cut relationship field. Close this item when an additional catalog
adapter exposes that field and a provider fixture verifies edge direction and raw
provenance.

## Milestone 7 — TV / episodic subtype

Status: Partial. TVMaze and TMDB season/episode records, AniList schedules, and
upcoming queue entries are supported. TVMaze and TMDB episodes with the same
series entity and season/episode position now merge into one watched item while
retaining both providers' IDs and release observations. Cross-format identity and
relationship coverage still need broader fixtures.

- [x] TMDB, TVMaze, AniList, and Wikidata metadata paths
- [x] TVMaze seasons, specials, episodes, and next-airing records
- [x] TMDB seasons and episodes with provider-neutral season/episode identity
- [x] Merge same-position TVMaze/TMDB episode watches when both catalogs link to one series entity
- [x] Policy-controlled episode watches and upcoming search placeholders
- [x] On release day, episode watches search configured Video IRC sources and download matching video files
- [x] Preserve ongoing, completed, cancelled, hiatus, and unscheduled catalog states as temporal claims
- [x] Reconcile same-position TVMaze/TMDB episode-date conflicts and schedule the reviewed date
- [x] Let review choose a provider's canonical lifecycle status while retaining all competing claims
- [x] Apply accepted renewal, cancellation, and hiatus announcements to canonical series status with event evidence
- [x] Automatically reconcile a strict majority lifecycle status from at least three independent catalog providers; ties remain in Needs Review
- [x] Keep AniList movie sequels and TVMaze/TMDB specials inside the shared Video graph; preserve AniList sequel/prequel/spin-off edge direction and raw relation types
- [x] Apply separate sequel/prequel watch policies to AniList and Wikidata work versus series targets; reserve the future-installment policy for explicitly future-dated sequel works
- [x] Model seasons, regular episodes, and specials with `season_of`, `episode_of`, and `special_of` edges while retaining provider membership evidence
- [x] Give Video specials an independent watch-policy setting; when no special override exists, catalog and feed watches inherit the existing episode behavior
- [x] Cross-catalog movie, special, and episodic relationships through TMDB collections/seasons, TVMaze specials, AniList sequels/prequels/spin-offs, and shared Video membership edges

## Milestone 8 — Web-series subtype

Status: Partial. RSS/Atom, JSON Feed, Schema.org JSON-LD, iCalendar, and YouTube feed
watches baseline stable IDs and structured episode dates/positions. Dedicated series
feeds can add new episodes; YouTube channel uploads stay as review candidates unless
the user explicitly enables episode auto-add. Sites without one of these formats or
a connected public API still need source-specific adapters.

YouTube playlist watches can optionally use the YouTube Data API key configured in
Settings to retain native playlist-item, video, channel, and position metadata. A
complete paginated snapshot records removed items as graph events and Needs Review
entries; the snapshot is capped at 5,000 items, and incomplete/API-failed scans never
infer removals. The API also checks the first 100 playlist entries for scheduled
premiere timestamps; a failed schedule lookup does not invalidate a complete playlist
snapshot. It also retains YouTube's ISO 8601 video duration and live broadcast status,
plus scheduled, actual-start, and actual-end timestamps where available. RSS remains the
fallback when the key is unavailable.

- [x] RSS/Atom feed refresh and episode release records
- [x] Discover advertised RSS, Atom, JSON Feed, and iCalendar links from creator homepages
- [x] JSON Feed creator-site refresh with stable item IDs, tags, summaries, dates, episode positions, and enclosure metadata
- [x] Use RSS GUID and Atom entry IDs to preserve feed-native episode identity
- [x] Use JSON schedule event types and start dates to review or schedule creator releases
- [x] Read Schema.org JSON-LD Event/ItemList feeds, including ScreeningEvent, PublicationEvent, and TVEpisode dates and positions
- [x] Expand nested Schema.org TVSeries/TVSeason episode lists and retain video duration, content URL, and thumbnail metadata
- [x] Baseline existing entries and add later episodes for dedicated series feeds
- [x] Normalize YouTube playlist page URLs to playlist RSS feeds
- [x] Preserve the episode baseline, seen IDs, and timestamps when a feed watch is re-added
- [x] Keep mixed YouTube channel uploads in review by default
- [x] Native playlist item metadata and removed-entry tracking for complete YouTube Data API snapshots
- [x] Use public YouTube Data API premiere timestamps to schedule upcoming playlist episodes
- [x] Merge explicitly numbered feed episodes (`S01E02`, `1x02`, or `Season 1 Episode 2`) into provider-neutral episode identity
- [x] Classify explicit trailer, teaser, bonus, behind-the-scenes, and announcement markers from feed types, titles, categories, and summaries before auto-adding feed entries
- [x] Accepting a creator-channel episode review creates and queues the corresponding episode watch
- [x] Preserve iCalendar event IDs and accept categorized, dated creator episode events into scheduled child watches
- [x] Classify explicit episode, trailer, teaser, bonus, announcement, and structured schedule-event markers across supported feed formats
- [x] Preserve nested creator-series, season, episode, and video identity/metadata from Schema.org feeds
- [ ] Add source-specific creator-platform adapters where a site exposes no compatible feed or shared API

Creator-page URLs now autodiscover advertised RSS, Atom, JSON Feed, and iCalendar
links. The remaining external dependency is a named platform whose pages cannot be
represented by those feeds or the existing YouTube API path; its public page/API
shape and representative response fixture are needed to build and verify a specific
adapter.

## Milestone 9 — Additional Video subtypes

Status: Partial. AniList anime formats normalize as anime movies, series, short
series, ONAs, specials, OVAs, and music videos. Wikidata search and watch refresh now
classify general short films, OAD/OVA, and independent animation. Podcast and YouTube
adapters retain their subtype-specific episode data. Site-specific short-form sources
and richer catalog metadata remain open.

- [x] AniList anime movie, series, short-series, ONA, special, OVA, and music-video types
- [x] RSS/Atom video-podcast, documentary, and performance series watches with distinct subtypes
- [x] Podcast RSS season/episode positions, categories, artwork, summaries, durations, and enclosure metadata
- [x] Normalize explicit Wikidata short-film, OAD, OVA, and independent-animation types
- [x] General short films, OADs, and independent animation discovery and watch normalization through Wikidata
- [x] Normalize Wikidata limited series, web series, television series, and anime subtypes while preserving their series entity type
- [x] Retain subtype-specific podcast and YouTube episode positions, schedules, durations, enclosures, and native IDs
- [ ] Subtype-specific adapters where shared Video metadata is insufficient

The remaining subtype adapter requires a concrete subtype and the fields or release
events that its current TMDB, TVMaze, AniList, Wikidata, feed, and YouTube paths do
not provide. Until that target is identified, shared feed and catalog normalization
covers the currently represented video subtypes without inventing source semantics.

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

## Webcomics ingestion and source search milestone (2026-09-29)

Comics metadata search now combines Comic Vine individual issues, Grand Comics Database, optional Metron, filtered Wikidata and existing Taddy. Matching catalog records retain alternate editions/field provenance; reboot runs and trades remain distinct, and individual issues sort first. Selected issues use existing Watched and shared approved download-source searches. The [Comics metadata report](docs/webcomics/COMICS_METADATA_SEARCH.md) records research, API limits, test coverage, live verification and a structured expansion backlog for details, pagination, provider-neutral series watchers and identity review.

Implemented a Webcomics tab, additive normalized source/series/entry/chapter/asset persistence, adapter and metadata registries, public feed/static/platform extraction, evidence-based chapters, durable incremental watches, polite conditional HTTP, parser-collapse protection, original-byte CBZ manifests, and manual chapter overrides. Existing watches, queues, rights settings and UI workers remain the integration points.

Bulk book-series additions now preview multiple selected series, preserve duplicate/future-book behavior, bound outstanding book work, and avoid full queue JSON scans. Approved download sources search concurrently through a shared pool and rank exact matches before preferences; public file-host resolution is shared by GetComics and generic detail connectors.

The [Webcomics roadmap](docs/webcomics/WEB_COMICS_ROADMAP.md) records dependencies, implementation files, research and acceptance criteria for every remaining phase. The [handoff](docs/webcomics/WEB_COMICS_HANDOFF.md) and [connector validation](docs/webcomics/DOWNLOAD_CONNECTORS_VALIDATION.md) distinguish completed tests from live availability and unimplemented/session-dependent capabilities.
