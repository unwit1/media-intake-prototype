# Metadata search quality: checks and fixes

Four live checks, 350 series in total, were run against the app's own category
search (`Search all connected sources`) across Books, Videos (TV and film),
Comics, Manga, Anime, Games and Light novels. Each check looked for:

- the expected series missing, or not first
- duplicate rows for one work
- editions, box sets and add-ons listed as separate works
- errors from the catalogs

Every problem was traced to a general cause and fixed for all series, not just
the one that showed it. This page catalogues those fixes.

| Check | Series | Command | Main themes found |
|---|---|---|---|
| 1 | 50 | `python tools/metadata_check.py` | Wikidata labels, Steam editions, TV/web series splits, fuzzy matches first |
| 2 | 100 | `--more` | Wikidata throttling, organisations as authors, comic series buried under issues |
| 3 | 100 | `--third` | descriptive series names, box sets, romaji vs English titles, doujinshi |
| 4 | 100 | `--fourth` | nicknames, fan fiction, foreign organisations, omnibuses, giveaways |

Between checks 1 and 2, its leftovers (Saga, Arcane, rate limits) were fixed
and re-checked: deep comic runs, video ID merging and catalog pacing.

Results are written to `build/*.json`. `--only CATEGORY` limits a run and
`--resume` continues an interrupted one. Unit tests for the fixes are in
[`tests/test_metadata_dedup.py`](../tests/test_metadata_dedup.py).

## Contents

- [Books](#books)
- [TV and film](#tv-and-film)
- [Comics](#comics)
- [Manga, anime and light novels](#manga-anime-and-light-novels)
- [Games](#games)
- [All categories](#all-categories)
- [Rate limits and catalog availability](#rate-limits-and-catalog-availability)
- [Known limits](#known-limits)

---

## Books

| Problem | Example | Fix | Where |
|---|---|---|---|
| Wikidata IDs shown as authors or titles | Harry Potter by "Q34660"; a book titled "Q5813009" | English labels, falling back to the multilingual label; rows whose only label is a bare ID are skipped | `watched._wikidata_series_members` |
| Translations listed as extra books | "Kampen om jerntronen" in A Song of Ice and Fire | Members described as a translation, edition, reprint, omnibus or box set of another work are skipped | `watched._EDITION_MEMBER` |
| Series described with an unusual word not recognised | The Hunger Games ("pentalogy of books") | Duology, tetralogy, pentalogy, hexalogy, heptalogy, quartet and quintet are recognised | `watched._BOOK_SERIES_DESCRIPTION` |
| Common-word series not found | Foundation (Asimov) buried under foundations and a company | When no series matches, Wikidata is asked for "*name* series" | `watched._search_wikidata_series` |
| Series named with a descriptive word not matched | "Dune Chronicles", "The Silo Series", "Magic Tree House series" | Series names are compared without a leading article or a generic trailing word (series, saga, cycle, trilogy, chronicles, books, novels, collection…) | `discovery._series_name_key`, `watched._series_words_removed` |
| Series with no matching Wikidata label | "The Millennium Trilogy" | Falls back to Wikipedia's full-text search, which returns the same Wikidata items | `watched._search_wikidata_series`, `wikidata.search_wikipedia_entities` |
| Comic series appearing among book series | "Example Comic Book Series" | Comic, manga, manhwa and graphic-novel series are left to their own categories | `watched._is_book_series` |
| A series with one or two linked books hid everything else | Magic Tree House showed 2 results | Series with fewer than 3 linked books keep the other catalogs' results too | `discovery._expand_book_series_results` |
| Organisations listed as authors | "Nuffield Foundation.", "International Conference on the Three Body Problem", "Robert Bosch Stiftung", "Friends of the Earth Middle East" | Corporate names are dropped from author results, in English, German, French, Spanish, Italian and Portuguese | `sources._CORPORATE_AUTHOR` |
| One-off records listed as authors | "Bridgerton Fans" | Authors with fewer than 3 works are skipped | `sources.MIN_AUTHOR_WORKS` |
| Unrelated people ahead of the books | "Hieronymus Bosch" first for the Bosch novels | An author leads only when the search is that author's name | `discovery.AUTHOR_NAME_MATCH` |
| Box sets and multi-book volumes listed as books | "Graphix #1-4 Box Set", "Anne McCaffrey Boxed Set", "The Magic Tree House Collection #1", "The second Inspector Morse omnibus", "The City Watch Trilogy", "Legends II [11 works]" | Dropped from book results | `discovery._BOX_SET` |
| Edition listings as separate books | "Shift Deluxe Collector's Edition", "The Girl with the Dragon Tattoo (Millennium Trilogy) by Stieg Larsson (2008) Hardcover" | Edition labels are ignored when deciding whether two rows are the same work; the plainest title is shown | `discovery._EDITION_LABEL` |
| Google Books always "429 Too Many Requests" | Every book search | Optional free API key in Settings → Metadata APIs (saved encrypted) or `GOOGLE_BOOKS_API_KEY` | `sources.search_google_books_metadata` |

## TV and film

| Problem | Example | Fix | Where |
|---|---|---|---|
| One show listed twice | Stranger Things: TVMaze calls it a web series, Wikidata a TV series | Web series, miniseries and TV series are one kind when merging | `discovery._merge_same_title` |
| Same work under different titles not merged | "Arcane" (Wikidata) and "Arcane: League of Legends" (TVMaze) | Rows sharing an IMDb, TVMaze, TMDB or TheTVDB ID merge; Wikidata IDs come from one SPARQL query per search, TVMaze's from its externals | `discovery._cross_catalog_ids`, `wikidata.wikidata_external_ids` |
| Same-titled works indistinguishable | John Wick the film, the franchise and the comic | The year comes from Wikidata's description, and results show type and year ("Film · 2014", "Franchise", "TV series · 1966") | `discovery._DESCRIPTION_YEAR`, `gui._result_creator_label` |
| Other media listed as videos | A comic series, a soundtrack album, a single TV season | Comics, albums, games, novels, seasons and episodes are skipped in Videos and Anime | `discovery._NOT_A_VIDEO` |
| Fuzzy matches first | "Cane" and "Crane" above Arcane; "Rocky Road" above the film Rocky | Results are ordered exact match, then prefix, then all words present, then fuzzy; a promoted series cannot jump a closer match | `discovery._title_relevance`, `discovery._sort_by_title_relevance` |
| Films vanished when Wikidata was busy | Pirates of the Caribbean, Super Mario: 0 results | Wikidata search falls back to Wikipedia's search on 429/503 | `wikidata.search_wikidata` |

## Comics

| Problem | Example | Fix | Where |
|---|---|---|---|
| A famous run missing | Gaiman's *Sandman*, filed by GCD as "Sandman" (1989) | GCD is searched without the leading article | `comic_metadata.search_gcd` |
| An exact run pages deep in GCD's results | *Saga* among 568 "saga" matches | GCD lists names in sort order, so its pages are binary-searched for the exact name (a few requests at most) | `comic_metadata._gcd_exact_series` |
| Trade paperbacks shown as a second copy of every issue | Invincible, Saga | Runs GCD files as "Collected Editions" or "Collected Series" list as collections ("Invincible Vol. 3") | `comic_metadata.COLLECTION` |
| Translated reprints | "Biblioteca Sandman", "Den absoluta Sandman" | Foreign-language runs show only when no English run matched | `comic_metadata.search_gcd` |
| Series buried under dozens of issues | Bone, Preacher, Y: The Last Man | A plain series-name search leads with that series, longest run first; "Bone #3" still leads with the issue | `comic_metadata.sort_comics` |
| Same-named runs indistinguishable | Bone 1991, 1995 and 1997 across publishers | Results show "Comic series · 1991 · 45 issues" | `gui._result_creator_label` |
| Promotional samplers | "Free Scott Pilgrim Free Comic Book Day Edition" | Free Comic Book Day editions are left out unless searched for | `discovery.search_media_category` |
| Rate limit message unhelpful | "try again later" | Says how many minutes GCD's limit lasts | `comic_metadata._memo` |
| Quota used up quickly | ~6 comic searches in a row | Issues load for the best run only | `comic_metadata.search_gcd` |

## Manga, anime and light novels

| Problem | Example | Fix | Where |
|---|---|---|---|
| English and romaji titles as separate works | "Kaguya-sama: Love is War" and "Kaguya-sama wa Kokurasetai" | AniList returns every name (English, romaji, native, synonyms); manga and light-novel rows merge when one's title is another's alternate name, joining groups a row links | `comics.search_anilist`, `discovery._merge_same_title` |
| Same work across catalogs not merged | *Mobile Suit Gundam* on AniList and Wikidata | AniList, MyAnimeList and MangaDex IDs link rows, including Wikidata's AniList/MAL properties | `discovery._CROSS_CATALOG_IDS` |
| Edition variants as separate works | "Doujin Edition", "Special Edition", "(Official Colored)", "(Fan Colored)" | Folded into the work | `discovery._WORK_EDITION_SUFFIX` |
| Fan-made doujinshi among official works | "Kaguya-sama: Love is Destruction (Doujinshi)" | Left out unless the search asks for doujinshi | `discovery.search_media_category` |
| Spin-offs ahead of the main series | Code Geass specials before *Lelouch of the Rebellion* | Equal title matches go most-followed first (AniList popularity) | `discovery._sort_by_title_relevance` |
| Main entry missed under its romaji title | JoJo, Gin Tama | Relevance uses every name a work goes by | `discovery._sort_by_title_relevance` |
| Nicknames found nothing official | "DanMachi", "Oregairu" led by fan fiction | The top hit of AniList, RanobeDB or MangaUpdates (which resolve nicknames) counts as a strong match; MangaDex and LNORI are excluded because their top hit is often unrelated | `discovery._ALIAS_MATCHING_CATALOGS` |
| Fan fiction ahead of the official work | "Danmachi: RNG System" | Fan-fiction archive rows follow official entries at an equal match | `discovery._FAN_FICTION_ARCHIVES` |

## Games

| Problem | Example | Fix | Where |
|---|---|---|---|
| Store editions as separate games | "Mass Effect™ Legendary Edition", "The Witcher: Enhanced Edition Director's Cut", "Mass Effect 2 (2010) Edition" | Trademark marks, years and edition suffixes are removed so editions merge with the catalog's game | `media_providers.steam_base_title` |
| Add-ons as games | Soundtracks, recruit packs, "Digital Deluxe Edition Upgrade", "Collaboration Set" | Dropped from game results | `media_providers._STEAM_EXTRA` |
| Compilations as games | "Mass Effect Trilogy" | Wikidata compilations are skipped | `discovery._query_wikidata` |

## All categories

| Problem | Example | Fix | Where |
|---|---|---|---|
| Spelling differences split one work | "Ren'ai" / "Renai", "Tensai-tachi" / "Tensaitachi" | Title keys join words split by an apostrophe or hyphen | `discovery._title_key` |
| Spacing differences missed matches | "spider man" vs "Spider-Man" | Relevance also compares with spaces removed (6+ letters, so "Bone" ≠ "Bones") | `discovery._title_relevance` |

## Rate limits and catalog availability

| Problem | Fix | Where |
|---|---|---|
| Brief 429/503 answers dropped a catalog | `http_get` retries twice, honouring Retry-After up to 5 s | `core.http_get` |
| Bursts to Wikidata, Wikipedia and Google Books | One request at a time per host with a short gap; identical requests answered from memory for 5 minutes | `core.PACED_HOSTS` |
| Wikidata label search throttled | Falls back to Wikipedia's search | `wikidata.search_wikidata` |
| GCD quota | Exact-run page search is bounded, issues load for one run, and the wait is reported | `comic_metadata` |

Two fixes outside search came from the same work:
- A download checkpoint save could fail on Windows with "Access is denied" while the progress display read it. It now retries briefly (`transfer._save_state`).
- The check tool crashed on non-ASCII titles such as "Shōgun" when output went to a file. It now writes UTF-8.

## Known limits

- **Back-to-back searching:** Wikimedia (Wikidata and Wikipedia) and GCD
  throttle heavy use. Dozens of searches in a row, as the check tool does, can
  briefly leave films or comics short. Single searches are unaffected.
- **Google Books:** results only appear with an API key; without one Google
  answers 429 for every search.
- **Wikidata coverage:** some series have no book series item (Silo) or only a
  few linked books (Magic Tree House). Other catalogs' rows fill in, but there
  is no series bundle.
- **Split works:** AniList lists JoJo's Bizarre Adventure only as separate
  parts, so there is no single main entry to put first.
- **Distinct editions kept on purpose:** separate releases are kept as
  separate rows. Examples: "Street Fighter II: Champion Edition", the Bone runs
  from different publishers, and the 1987 Watchmen printings.
