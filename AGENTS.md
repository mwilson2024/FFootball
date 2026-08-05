# AGENTS.md

## Project: Full MFL Fantasy Draft Website

Build a complete local-first fantasy-football draft website for two MyFantasyLeague (MFL) leagues:

1. **Keeper league**
2. **Auction league**

The product must cover the full real-draft workflow: first-time setup, data synchronization,
league rosters, all-player research, a transparent multi-source cheat sheet, keeper planning,
live draft tracking, auction management, recommendations, and exports. Use the useful concepts
found in polished draft assistants such as FantasyPros as a feature benchmark, but do not copy
their design, branding, wording, proprietary rankings, or paid data.

Mock drafts and draft simulation are explicitly out of scope. Build for the user's actual MFL
leagues and actual draft-day decisions.

The application must use MFL's free API to load league settings, scoring rules, franchises, rosters, keepers, player information, rankings, ADP, auction values, and other available data. Rankings must be adjusted to each league's actual scoring and lineup configuration rather than assuming standard scoring.

The auction workflow must let the user assign a player to a franchise at a winning auction price, immediately update every team's remaining budget, prevent invalid purchases, and export the completed auction in both:

- a clean CSV using MFL player and franchise IDs; and
- an MFL-ready XML import file that mirrors the current MFL `auctionResults` schema.

> Important: MFL's documented commissioner import for offline auction results uses XML in the `DATA` parameter. Do not claim that a raw CSV can be uploaded directly to MFL unless the current league UI explicitly confirms a CSV importer. The CSV is the durable tabular export and the source for generating the MFL XML import payload.

---

## 1. Working Rules for Coding Agents

- Inspect the existing repository before changing architecture or dependencies.
- Reuse the current language, framework, database, and UI conventions when they already exist.
- When no application exists, use the default stack in this file.
- Implement working code, tests, migrations, and documentation—not only a design or pseudocode.
- Keep MFL IDs as strings. Never convert player, league, or franchise IDs to integers because leading zeroes may be significant.
- Do not hard-code league scoring, roster sizes, franchise names, budgets, or keeper rules.
- Do not commit usernames, passwords, API keys, login cookies, league IDs, or `.env` files.
- Never submit changes to MFL automatically. A commissioner import must require an explicit preview and confirmation action.
- Cache MFL responses according to their documented cache intervals.
- Space API calls and handle HTTP 429 responses with retry and exponential backoff.
- Send a descriptive `User-Agent` header with every request.
- Log useful errors without logging secrets or authentication cookies.
- Write unit tests without calling the live MFL API.

---

## 2. Default Technology Stack

Use these defaults only when the repository does not already establish another stack:

- Python 3.12+
- FastAPI
- SQLAlchemy 2.x
- Alembic
- SQLite for local development
- Pydantic Settings
- Jinja2 plus HTMX for the first UI
- HTTPX for MFL requests
- Pytest
- Ruff
- MyPy

Recommended layout:

```text
app/
  api/
  core/
  db/
  models/
  repositories/
  services/
    mfl/
    rankings/
    auction/
    exports/
  templates/
  static/
tests/
data/
exports/
alembic/
```

---

## 3. Configuration

Configure local values in an untracked `.env` file and production values in the deployment
platform. Do not add or recreate `.env.example`. The supported values are:

```dotenv
APP_ENV=development
DATABASE_URL=sqlite:///./data/fantasy_draft.db

MFL_SEASON=2026
MFL_KEEPER_LEAGUE_ID=
MFL_AUCTION_LEAGUE_ID=

# Read access may use a league API key when the league provides one.
MFL_KEEPER_API_KEY=
MFL_AUCTION_API_KEY=

# Commissioner login is needed only for protected data or an explicit import.
MFL_USERNAME=
MFL_PASSWORD=

MFL_USER_AGENT=MFLDraftManager/1.0 contact@example.com

# Use this only when the league settings do not expose the value reliably.
AUCTION_DEFAULT_BUDGET=200
AUCTION_MIN_BID=1

EXPORT_DIRECTORY=./exports
```

Validate required settings on startup. The application may start without commissioner credentials, but protected operations must be disabled and clearly labeled.

---

## 4. MFL API Client

Create a dedicated `MFLClient`. Do not scatter HTTP calls through routes or UI code.

### Base URL and host discovery

Use the selected season:

```text
https://api.myfantasyleague.com/{season}/
```

MFL may assign a league-specific host. Discover and retain the host returned by MFL for league-specific requests instead of assuming the public host for every call.

### Export request pattern

```text
GET {base}/{season}/export?TYPE={type}&L={league_id}&JSON=1
```

Add other parameters as required.

### Authentication

Support these modes:

1. Public read-only exports
2. League API key when available
3. MFL login cookie for private exports and commissioner imports

For login, use a POST request to the season login endpoint and retain the `MFL_USER_ID` cookie securely in memory or an encrypted session store. Never write the cookie to logs.

### Required export integrations

Implement typed parsers and cache policies for:

- `league`
- `rules`
- `allRules`, when useful
- `players`
- `rosters`
- `selectedKeepers`
- `playerRanks`
- `adp`
- `aav`
- `projectedScores`
- `auctionResults`
- `draftResults`
- `transactions`, when useful for reconciliation

### Import integration

Implement an optional commissioner-only import for:

- `auctionResults`
- `keepers`, when the user explicitly enables keeper submission

Use POST and send XML in the `DATA` parameter. Before importing:

1. fetch the latest MFL data;
2. validate the local state;
3. show the exact changes;
4. require explicit confirmation;
5. preserve a local export and audit record;
6. display the complete MFL response.

### Reliability

- Use connect, read, and total timeouts.
- Retry transient 5xx errors and 429 responses.
- Apply exponential backoff with jitter.
- Do not retry validation or authentication failures blindly.
- Cache stable data such as the player directory longer than live auction state.
- Store `fetched_at`, source endpoint, parameters, and season for every snapshot.

---

## 5. Domain Models

At minimum, implement the following entities.

### League

```text
id: string
season: int
league_type: keeper | auction
name: string
roster_size: int
starting_budget: decimal | null
minimum_bid: decimal
settings_json: json
scoring_rules_json: json
synced_at: datetime
```

### Franchise

```text
id: string
league_id: string
name: string
abbreviation: string | null
starting_budget: decimal
spent: decimal
remaining: decimal
roster_slots: int
slots_used: int
slots_remaining: int
maximum_bid: decimal
```

### Player

```text
id: string
name: string
position: string
nfl_team: string | null
status: string | null
birthdate: date | null
rookie: bool | null
```

### RankingSnapshot

```text
id
league_id
player_id
overall_rank
position_rank
tier
custom_score
projected_points
replacement_points
value_over_replacement
adp
mfl_rank
mfl_aav
suggested_auction_value
source_summary_json
created_at
```

### KeeperSelection

```text
id
league_id
franchise_id
player_id
keeper_cost: decimal | null
source: mfl | local
selected_at
```

### AuctionPurchase

```text
id
league_id
franchise_id
player_id
amount: decimal
status: ROSTER | TAXI_SQUAD | INJURED_RESERVE
purchase_order: int
source: local | mfl
created_at
updated_at
```

Enforce a unique constraint on `(league_id, player_id)` so one player cannot be sold twice.

### AuctionAuditEvent

Record create, edit, delete, undo, sync, export, and import events. Store before/after values and timestamps.

---

## 6. League Synchronization

Implement a single synchronization service that accepts either configured league.

For each league:

1. Load league metadata and franchises.
2. Load scoring rules.
3. Load lineup and roster requirements.
4. Load current rosters.
5. Load selected keepers when relevant.
6. Load the player directory.
7. Load MFL player ranks.
8. Load sitewide ADP.
9. Load sitewide average auction values.
10. Load projected scores when available.
11. Reconcile IDs without converting them to numbers.
12. Persist a dated snapshot.
13. Recalculate league-specific rankings.

A failed endpoint must not silently replace valid cached data with an empty result. Show stale-data indicators when the last successful snapshot is being used.

---

## 7. League-Specific Ranking Engine

The ranking engine must produce rankings for each league independently.

### Inputs

Use:

- MFL `playerRanks` as a free baseline ranking signal
- MFL `adp` as a market-cost signal
- MFL `aav` as an auction-market signal
- MFL `projectedScores` when available
- league scoring rules
- starting-lineup requirements
- flex and superflex eligibility
- roster size and bench depth
- number of franchises
- current rosters
- selected keepers
- position scarcity
- replacement-level player at each position

### Do not assume

Do not assume any of the following without reading the league:

- full PPR, half PPR, or standard scoring
- one quarterback
- no superflex
- a required tight end
- offense-only rosters
- standard defense or kicker settings
- a $200 budget
- twelve teams
- standard roster limits

### Ranking method

Use a transparent value-over-replacement model:

```text
custom_score =
    scoring_adjusted_projection
    - replacement_level_projection
    + positional_scarcity_adjustment
    + lineup_demand_adjustment
    + market_value_adjustment
```

The implementation may improve this formula, but it must remain documented and testable.

Determine replacement level from the league's actual number of franchises, required starters, flex demand, and reasonable bench demand. Handle superflex/2QB leagues separately because quarterback replacement level changes substantially.

### Scoring adjustments

Map every supported MFL scoring rule into normalized internal categories, including:

- passing
- rushing
- receiving
- first downs
- receptions
- bonuses
- turnovers
- kicking
- team defense
- individual defensive players
- return scoring
- position-specific scoring

Unknown scoring rules must be retained and surfaced in a warning. Do not silently ignore a rule that could change rankings.

### Projection honesty

Do not label a ranking as an exact season projection unless the data source actually supplies a season projection. When MFL provides weekly projected scores, identify them as weekly projections and document how they are aggregated or used.

### Ranking output

Each row must include:

```text
overall_rank
position_rank
tier
player_id
player_name
position
nfl_team
custom_score
projected_points
value_over_replacement
mfl_rank
adp
mfl_aav
suggested_auction_value
rostered
keeper
available
data_updated_at
```

Users must be able to sort and filter by position, team, tier, availability, ADP, auction value, and custom score.

---

## 8. Keeper League Workflow

The keeper league screen must:

- show every franchise and its current roster;
- show MFL-selected keepers;
- allow local keeper selections before submission;
- show keeper cost when the league uses auction dollars or draft-round costs;
- exclude confirmed keepers from the available-player ranking pool;
- recalculate positional scarcity after keepers are removed;
- identify discrepancies between local keeper choices and MFL;
- export keeper selections;
- optionally generate an MFL keeper import preview.

Never submit keeper changes automatically.

---

## 9. Auction League Workflow

Create a dedicated auction room.

### Main screen

Display:

- searchable and filterable available-player table;
- overall rank, tier, ADP, MFL AAV, and suggested league value;
- all franchises with starting budget, spent, remaining, slots remaining, and maximum legal bid;
- recent purchases;
- undo-last-action control;
- export status;
- MFL synchronization status.

### Assigning a player

The user must be able to:

1. select a player;
2. select a franchise;
3. enter the winning auction value;
4. choose roster status;
5. save the purchase.

The save must be atomic.

### Validation

Reject a purchase when:

- the player is already sold or rostered;
- the franchise does not exist;
- the amount is below the minimum bid;
- the amount exceeds the team's legal maximum bid;
- the franchise has no open roster slot;
- the amount has more precision than the league permits;
- the data is stale enough to create a conflict with MFL.

### Budget calculations

Use decimal arithmetic, not floating point.

```text
spent = sum(all active purchases for the franchise)

remaining = starting_budget - spent

slots_remaining = roster_size - active_players_owned

minimum_reserve =
    max(0, slots_remaining - 1) * minimum_bid

maximum_bid =
    max(0, remaining - minimum_reserve)
```

Example with a $200 budget, $1 minimum bid, $45 remaining, and 4 empty slots:

```text
minimum_reserve = 3
maximum_bid = 42
```

Recalculate balances and maximum bids after every create, edit, delete, import, or undo operation.

### Required auction actions

- Add purchase
- Edit franchise
- Edit amount
- Edit roster status
- Delete purchase
- Undo last mutation
- Redo when practical
- Import existing MFL auction results
- Clear local auction after confirmation
- Export CSV
- Export MFL XML
- Preview commissioner import
- Reconcile local results with MFL

Use database transactions and locking or optimistic concurrency to prevent two simultaneous requests from buying the same player.

---

## 10. Auction Value Recommendations

Generate league-specific suggested values after rankings are calculated.

### Requirements

- Total suggested values should respect the total league economy.
- Account for money already committed to keepers or pre-existing rosters.
- Reserve at least the minimum bid for every unfilled roster slot.
- Recalculate inflation as players are purchased.

Suggested approach:

```text
draftable_value_pool =
    sum(max(0, player_vorp) for available draftable players)

available_spending_pool =
    sum(franchise_remaining)
    - minimum_dollars_required_for_open_slots

player_raw_share =
    max(0, player_vorp) / draftable_value_pool

suggested_value =
    minimum_bid + player_raw_share * available_spending_pool
```

Then normalize values so the complete recommendation set does not exceed available spending. Show both:

- pre-auction baseline value;
- live inflation-adjusted value.

MFL AAV is a market reference, not the league-specific final value. Clearly distinguish them.

---

## 11. API Routes

Implement equivalent endpoints even if route names must follow an existing project convention.

```text
POST   /api/sync
GET    /api/leagues
GET    /api/leagues/{league_id}
GET    /api/leagues/{league_id}/rankings
GET    /api/leagues/{league_id}/franchises
GET    /api/leagues/{league_id}/keepers

GET    /api/auction/state
POST   /api/auction/purchases
PATCH  /api/auction/purchases/{purchase_id}
DELETE /api/auction/purchases/{purchase_id}
POST   /api/auction/undo
POST   /api/auction/redo

GET    /api/auction/export.csv
GET    /api/auction/export.xml
GET    /api/auction/import-preview
POST   /api/auction/push-to-mfl
```

`push-to-mfl` must be disabled unless commissioner credentials are configured and the user submits an explicit confirmation token.

---

## 12. CSV Export

Generate:

```text
exports/mfl_auction_results_{league_id}_{season}.csv
```

Use UTF-8, comma delimiters, a header row, normal CSV quoting, and no currency symbols.

Required columns:

```csv
league_id,season,franchise_id,franchise_name,player_id,player_name,position,nfl_team,auction_value,status,purchase_order
```

Rules:

- Keep IDs as strings.
- Preserve leading zeroes.
- Use a plain decimal for `auction_value`.
- Use one of these status values:
  - `ROSTER`
  - `TAXI_SQUAD`
  - `INJURED_RESERVE`
- Sort by `purchase_order`.
- Reject duplicate player IDs before export.
- Include only active purchases.
- Write exports atomically through a temporary file and rename.
- Also generate a SHA-256 checksum and an export manifest.

The CSV is the canonical human-readable audit file and can be used to regenerate the MFL XML import payload.

---

## 13. MFL XML Export and Import

Generate:

```text
exports/mfl_auction_results_{league_id}_{season}.xml
```

Do not guess the XML structure.

Before implementing the serializer:

1. call the current season's MFL `auctionResults` export for a league with at least one result, when available;
2. inspect the exact returned XML field and element names;
3. mirror the corresponding export schema required by MFL's import;
4. add a versioned fixture to the test suite;
5. preserve string IDs and decimal auction values;
6. validate the completed document before import.

When no live result is available, implement the serializer behind a feature flag and require a captured, redacted MFL response fixture before enabling commissioner upload.

For commissioner import:

- use MFL's `auctionResults` import endpoint;
- send XML through `DATA`;
- use `CLEAR=1` only after explicit confirmation;
- use `OVERWRITE=1` only after showing its effect;
- warn that overwriting without clearing can create inconsistent franchise funds;
- preserve the MFL response and the exact submitted XML in the audit log.

---

## 14. UI Requirements

The initial UI must work on a desktop browser and remain usable on a tablet.

### Auction table

Columns:

```text
Rank | Tier | Player | Pos | NFL | Custom Score | ADP | MFL AAV |
Suggested Value | Sold To | Price | Action
```

### Franchise budget panel

For each team:

```text
Franchise
Players: used / total
Starting budget
Spent
Remaining
Maximum bid
```

Highlight invalid or over-budget states prominently. Do not rely on color alone.

### Quality-of-life behavior

- keyboard-friendly player search;
- fast franchise selection;
- confirmation message showing player, team, and amount;
- visible undo action after every sale;
- no full-page refresh after a purchase;
- downloadable export buttons;
- responsive error messages with a corrective action.

---

## 15. Tests

### MFL client tests

- host discovery
- public export
- API-key export
- login-cookie handling
- 401/403 behavior
- 429 retry behavior
- 5xx retry behavior
- JSON and XML parsing
- cache expiration
- no secret leakage in logs

### ID tests

- preserve player ID `"0001234"`
- preserve franchise ID `"0001"`
- preserve league ID as a string
- CSV does not coerce IDs to numbers

### Ranking tests

- PPR changes receiving-player values
- superflex raises quarterback replacement value
- tight-end premium changes tight-end values
- keeper removal changes replacement level
- rostered players are unavailable
- unknown rules produce warnings
- rankings are deterministic from a fixed snapshot

### Auction tests

- add a legal purchase
- reject duplicate player
- reject an amount below the minimum
- reject an amount above maximum bid
- recalculate remaining budget
- recalculate maximum bid
- edit and delete purchase
- undo mutation
- prevent negative balance
- enforce roster slots
- concurrent duplicate-purchase protection
- decimal precision

### Export tests

- exact CSV headers
- UTF-8 output
- CSV round trip
- IDs retain leading zeroes
- duplicate detection
- valid status values
- deterministic ordering
- XML validates against captured MFL fixture
- import preview matches local state

---

## 16. Acceptance Criteria

The feature is complete only when all of the following are true:

1. Both configured MFL leagues synchronize successfully.
2. Scoring rules and lineup requirements are visible for each league.
3. Each league has its own custom ranking list.
4. Keeper players are marked and removed from the available pool.
5. The auction room can assign a player to a franchise at a price.
6. Remaining budget, roster slots, and maximum bid update immediately.
7. Duplicate players and illegal bids are blocked.
8. Purchases can be edited, deleted, and undone.
9. The application exports a valid CSV with MFL IDs.
10. The application generates an MFL XML import payload from the same auction data.
11. Commissioner upload is previewed and explicitly confirmed.
12. Tests cover ranking logic, budget logic, ID preservation, and exports.
13. Secrets are not committed or exposed.
14. README instructions allow a new developer to run the project locally.

---

## 17. Implementation Order

Follow this order unless the repository requires a justified variation:

1. Inspect repository and document current architecture.
2. Add configuration without committing environment or secret files.
3. Implement the typed MFL client.
4. Add database models and migrations.
5. Implement league synchronization.
6. Persist scoring, roster, player, keeper, ADP, AAV, and rank snapshots.
7. Build the scoring-aware ranking engine.
8. Build keeper views.
9. Build auction state and budget service.
10. Build auction UI.
11. Add CSV export.
12. Capture the current MFL `auctionResults` XML schema.
13. Add XML export and import preview.
14. Add optional commissioner submission.
15. Complete tests, README, and sample screenshots.

---

## 18. Definition of Done for Every Change

Before declaring work complete:

```bash
ruff check .
ruff format --check .
mypy app
pytest
```

Also verify manually:

- both leagues load;
- ranking filters work;
- a purchase updates the correct team;
- the maximum bid calculation is correct;
- undo restores the previous state;
- CSV opens correctly without losing leading zeroes;
- XML preview matches the local auction;
- no MFL write occurs without a confirmation action.

---

## 19. Official MFL References

Use the current-season versions of MFL's official API documentation:

```text
https://api.myfantasyleague.com/2026/api_info
https://api.myfantasyleague.com/2026/api_info?STATE=details
```

Prefer official MFL documentation over third-party assumptions. When the API response and this file differ, preserve backward compatibility when practical, document the discrepancy, and follow the current official MFL contract.

---

## 20. Expanded Product Direction

The application must be a complete drafting website, not only an auction-entry form. A user
should be able to open the site before a draft, understand the entire league, prepare a personal
board, and keep that board useful throughout the real draft.

Primary jobs:

1. Connect and verify the two real MFL leagues through a guided setup screen.
2. Show every league franchise and every player currently owned by that franchise.
3. Show the complete relevant player pool, not only players that have a ranking row.
4. Build a transparent average/consensus cheat sheet from every legally usable configured source.
5. Adjust the consensus to each league's scoring, lineup, roster, keeper, and availability rules.
6. Track actual draft picks or auction purchases in real time.
7. Recommend useful next choices using availability, tiers, scarcity, roster needs, and value.
8. Export all important views and preserve a local audit trail.

Explicitly out of scope:

- mock drafts;
- simulated opponents or bot drafting;
- copying FantasyPros pages, rankings, visual design, or proprietary terminology;
- scraping a provider that does not grant permission;
- bypassing subscriptions, authentication, rate limits, or robots/terms restrictions;
- automatic commissioner writes without preview and confirmation.

The existing keeper and auction requirements remain in force. When a requirement below expands a
narrower earlier requirement, the expanded requirement controls.

---

## 21. Data Source Strategy

Create a source-adapter layer. Each source must have its own client, parser, cache policy, health
status, license/terms note, attribution text, and last-successful-fetch time. The UI must never
present blended data without showing which sources contributed.

### 21.1 MFL: authoritative league source

MFL remains authoritative for:

- league identity and season;
- scoring and lineup requirements;
- franchises and franchise names;
- rosters and player ownership;
- selected keepers;
- real draft and auction results;
- MFL player IDs;
- league-specific projected scores when available;
- MFL player ranks, ADP, and AAV;
- commissioner imports.

If an enrichment source conflicts with MFL about ownership, keeper state, franchise state, draft
state, or an MFL ID, MFL wins and the discrepancy is shown.

### 21.2 Sleeper: optional free enrichment

Sleeper provides a documented, free, read-only API with no token requirement. Use it only as an
optional enrichment source for:

- active player directory and fantasy positions;
- injury/practice/status metadata when present;
- depth-chart metadata when present;
- cross-platform player IDs;
- add/drop trending signals;
- optional comparison rosters when the user deliberately configures a Sleeper league.

Do not treat Sleeper trending counts as expert rankings or projections. Label them as market
activity. Cache the full player directory for at least 24 hours and remain comfortably below the
documented request guidance.

Official reference:

```text
https://docs.sleeper.com/
```

### 21.3 nflverse and ffverse: optional open-data enrichment

Use nflverse data, preferably through the maintained Python loader `nflreadpy` or documented
release URLs, for legally reusable data such as:

- player identity crosswalks;
- current and historical NFL rosters;
- historical weekly/season statistics;
- schedules and bye weeks;
- depth charts;
- expected opportunity/fantasy-points features when available;
- reproducible inputs for an internal projection or risk model.

Store dataset version, release URL, license, attribution, and fetched timestamp with every import.
Do not imply historical production is a current projection.

The nflverse `load_ff_rankings()` dataset may expose rankings originating from FantasyPros or
another upstream provider. It must be disabled by default until the exact current upstream terms,
redistribution rights, attribution, and intended use have been reviewed and recorded in the source
registry. Never scrape FantasyPros directly.

Official references:

```text
https://github.com/nflverse/nflverse-data
https://github.com/nflverse/nflreadpy
https://nflreadr.nflverse.com/
```

### 21.4 User-provided rankings

Support CSV import as a first-class ranking source. A user must be able to import one or more
personal or legally obtained sheets with columns such as:

```text
player_name,team,position,overall_rank,position_rank,tier,projection,auction_value,source
```

Show a mapping preview before import. Reject ambiguous player matches until the user resolves them.
Preserve the original file, checksum, import time, source label, and all manual mapping decisions.

### 21.5 Sources not to use by default

Do not depend on undocumented ESPN endpoints, scraped website HTML, copied paid rankings, or an API
whose terms cannot be established. A connector can be added only after documenting its official
contract, reliability, rate limit, attribution, and permitted use.

---

## 22. Player Identity and Source Provenance

Add a `PlayerIdentity` model that maps one canonical local player to provider IDs without replacing
the MFL string ID:

```text
player_id: local canonical string
mfl_id: string | null
gsis_id: string | null
sleeper_id: string | null
espn_id: string | null
fantasypros_id: string | null
other_ids_json: json
match_method: exact_id | crosswalk | exact_name_team | manual
match_confidence: decimal
verified: bool
updated_at: datetime
```

Never silently join solely by a normalized name when two plausible matches exist. Normalize suffixes,
punctuation, team abbreviations, defense/team-player records, and common position aliases. Provide an
identity-resolution screen for unresolved or low-confidence matches.

Every ranking, projection, market value, injury note, and trend must retain:

```text
source_name
source_version
source_player_id
source_updated_at
fetched_at
license_or_terms_url
raw_value
normalized_value
```

---

## 23. Consensus Cheat Sheet

Create a dedicated consensus engine and cheat-sheet screen. “Consensus” means a documented blend of
the available configured inputs; it must never imply an official FantasyPros Expert Consensus Rank.

### 23.1 Inputs

Eligible inputs include:

- MFL player rank;
- MFL ADP;
- MFL AAV for auction context;
- MFL scoring-adjusted projected scores;
- approved open ranking feeds;
- approved open projection feeds;
- user-uploaded rankings and projections;
- the application's scoring/VOR model;
- Sleeper add/drop trends as a separately labeled market-momentum feature;
- historical performance and opportunity features as separately labeled model inputs.

Do not average incompatible values directly. Rankings, ADP, projections, auction dollars, and trend
counts must first be normalized within their own type.

### 23.2 Normalization and aggregation

For ordinal rankings:

1. preserve the raw rank;
2. convert each source to a percentile using that source's actual populated player pool;
3. calculate both weighted mean percentile and median percentile;
4. calculate source count, minimum rank, maximum rank, range, and dispersion;
5. ignore missing values rather than treating them as zero or last place;
6. apply configurable source weights that default to equal weights;
7. use deterministic tie-breaking by median rank, league value, then MFL player ID.

For projections:

1. retain each raw projection and scoring basis;
2. translate only when the underlying stat categories are available;
3. do not convert a weekly projection into a season projection without an explicit documented method;
4. normalize projections after applying the target league's actual rules;
5. show uncertainty or source disagreement.

The default board should expose at least:

```text
consensus_rank
league_adjusted_rank
position_rank
tier
player
position
nfl_team
availability
rostered_by
keeper
source_count
average_rank
median_rank
best_rank
worst_rank
rank_range
custom_score
projected_points
value_over_replacement
adp
mfl_aav
baseline_auction_value
live_auction_value
injury_status
bye_week
trend
data_freshness
```

### 23.3 Tiers and disagreement

- Generate default tiers from statistically meaningful gaps in consensus/league value.
- Let users drag or edit tier boundaries and preserve those changes.
- Highlight players with high source disagreement as volatile, not automatically bad.
- Show how many players remain in each tier and position.
- Show the drop from the current player to the next tier at the same position.

### 23.4 Personalization

Users must be able to:

- change source weights;
- exclude a source;
- create manual player overrides;
- reorder players with drag-and-drop or keyboard controls;
- assign personal tiers;
- add notes, tags, targets, fades, and do-not-draft status;
- create and reorder a draft queue/watchlist;
- reset to the generated consensus without losing the saved manual version;
- save named cheat-sheet snapshots;
- export the board to CSV and printer-friendly PDF/HTML.

---

## 24. Required Website Navigation and Pages

Use a persistent desktop navigation layout with clear tablet behavior. At minimum provide:

1. **Home / League Dashboard**
2. **Draft Room**
3. **Cheat Sheet**
4. **All Players**
5. **League Rosters**
6. **Franchise Detail**
7. **Keepers**
8. **Auction Room**
9. **Data Sources**
10. **Settings / MFL Connection**

The app must not require users to know route URLs.

### 24.1 Home / league dashboard

Show both configured leagues, connection status, league type, franchise count, roster size, scoring
summary, draft date/status when available, last successful sync, stale sources, unresolved players,
and a prominent action to enter the relevant draft room.

### 24.2 All Players

This page must show every relevant MFL player even when no external ranking exists. Use server-side
pagination or table virtualization so thousands of players remain responsive.

Required filters:

- text search;
- fantasy position, including multi-position eligibility;
- NFL team and free agent;
- available, rostered, drafted, keeper, or all;
- owning franchise;
- rookie/veteran;
- injury/practice status;
- active/inactive player status;
- tier;
- bye week;
- source coverage count;
- minimum/maximum ADP, rank, projection, or auction value;
- targets, fades, queued, and do-not-draft tags.

Filters must be combinable, bookmarkable through query parameters, keyboard accessible, and retained
per user/device. Provide clear-all and active-filter chips.

### 24.3 League Rosters

Provide both:

- a franchise grid showing every team's complete roster; and
- a sortable roster table with franchise, player, position, NFL team, status, salary/cost, keeper,
  contract information when present, and league-adjusted rank.

For each franchise show:

- used and open roster slots;
- starters versus bench when known;
- counts by position;
- keepers and their costs;
- drafted/purchased players;
- positional needs relative to lineup requirements;
- auction starting budget, spent, remaining, reserve, and maximum bid when applicable;
- roster-strength summary based on the same league-adjusted values used in the cheat sheet.

Clicking a franchise opens a detail page. Clicking a player opens a player drawer/detail page without
losing the user's current filters.

### 24.4 Player detail

Show identity, team, positions, status, ownership, keeper state, all raw source values, consensus,
league adjustment, tier, VOR, notes, trend, historical production, and data timestamps. Explain every
derived recommendation in plain language.

---

## 25. Real Draft Room (No Mock Drafts)

Add a live draft room for the user's actual non-auction draft. It can follow MFL automatically when
current draft results are available and must also support manual entry when live MFL data is delayed.

Display:

- current pick/round and on-the-clock franchise when available;
- recent picks;
- the full league-adjusted available-player board;
- personal queue/watchlist;
- every franchise's roster construction;
- remaining player counts by position and tier;
- team needs;
- suggested next players with explanations;
- MFL sync status and last-update age;
- undo/reconcile controls for local manual entries.

Required actions:

- record a real pick manually;
- edit or delete a mistaken manual pick;
- mark a player as drafted without assigning a franchise only when franchise data is unknown;
- queue/unqueue players;
- add a note or tag;
- sync and reconcile with MFL `draftResults`;
- resolve local-versus-MFL conflicts;
- export final results and a draft recap.

Use optimistic concurrency and unique constraints so a player cannot be drafted twice. MFL remains
authoritative after an explicit reconciliation. Do not poll aggressively; follow MFL cache and rate
guidance, suspend polling when the tab is hidden, and back off after failures.

---

## 26. Draft Recommendations

Recommendations should follow the useful principles of modern draft assistants while remaining
transparent. Provide multiple views instead of one unexplained “best pick”:

- best available overall;
- best available at each position;
- value versus ADP/market rank;
- team need;
- positional scarcity;
- tier-cliff urgency;
- roster construction and remaining required starters;
- superflex/2QB demand;
- auction value and live inflation;
- optional bye-week concentration warning;
- optional injury/availability risk;
- user targets and fades.

For every suggested player, show a short reason such as:

```text
Highest remaining league value; 9.4 points above the next replacement RB;
last player in Tier 2; your roster still needs two RB starters.
```

Recommendations are advisory. Never hide the complete player pool or force the user to follow a
recommendation.

---

## 27. Setup, Authentication, and Data-Source Health UI

The app must include a first-run setup experience so configuration does not require editing files.
Environment variables remain supported and take precedence where appropriate.

The setup screen must allow the user to:

1. choose the MFL season;
2. enter keeper and auction league IDs;
3. test each league ID and show the returned league name/host;
4. classify or confirm each league's role;
5. enter a league-scoped API key without displaying it after save;
6. verify public and protected read access independently;
7. see which endpoints are unavailable and why;
8. configure the descriptive User-Agent/client registration value;
9. enable optional Sleeper and nflverse sources;
10. review cache age, last success, last error, and attribution for every source.

Secrets must be stored using the operating-system credential store when available. If a local file
fallback is unavoidable, restrict permissions, keep it outside version control, redact it from all
responses/logs, and warn the user. Commissioner passwords should preferably be session-only. Never
send secrets to browser analytics or include them in support bundles.

Clearly distinguish:

- MFL league API key: owner-level protected exports only;
- MFL username/password cookie login: private/commissioner access;
- registered API client User-Agent: higher request limits, not authentication.

---

## 28. Additional Domain Models

Add or equivalent:

### DataSource

```text
id
name
kind: league | ranking | projection | market | metadata | historical
enabled
weight
terms_url
license
attribution
cache_ttl_seconds
last_attempt_at
last_success_at
last_error
```

### SourcePlayerValue

```text
id
source_id
league_id: string | null
player_id
value_type: rank | adp | projection | auction_value | trend | injury | other
raw_value_json
normalized_value
source_updated_at
fetched_at
snapshot_id
```

### ConsensusSnapshot

```text
id
league_id
name
source_weights_json
formula_version
created_at
```

### PersonalPlayerPreference

```text
id
league_id
player_id
manual_rank
manual_tier
queue_order
target
fade
do_not_draft
notes
tags_json
updated_at
```

### DraftSession and DraftPick

```text
DraftSession:
  id
  league_id
  season
  status
  current_round
  current_pick
  source
  synced_at

DraftPick:
  id
  session_id
  league_id
  player_id
  franchise_id: string | null
  round: int | null
  pick: int | null
  overall_pick: int | null
  source: local | mfl
  selected_at
  version
```

Enforce uniqueness for a player within a real league draft session.

---

## 29. Expanded API Routes

Implement equivalent endpoints following established project conventions:

```text
GET    /api/setup/status
POST   /api/setup/test-mfl
PUT    /api/setup/leagues
PUT    /api/setup/sources/{source_id}
GET    /api/sources
POST   /api/sources/sync

GET    /api/players
GET    /api/players/{player_id}
GET    /api/players/filters
GET    /api/player-identities/unresolved
PATCH  /api/player-identities/{player_id}

GET    /api/leagues/{league_id}/rosters
GET    /api/leagues/{league_id}/franchises/{franchise_id}
GET    /api/leagues/{league_id}/cheat-sheet
POST   /api/leagues/{league_id}/cheat-sheet/recalculate
POST   /api/leagues/{league_id}/cheat-sheet/import
GET    /api/leagues/{league_id}/cheat-sheet/export.csv
PATCH  /api/leagues/{league_id}/preferences/{player_id}

GET    /api/draft/state
POST   /api/draft/picks
PATCH  /api/draft/picks/{pick_id}
DELETE /api/draft/picks/{pick_id}
POST   /api/draft/reconcile
POST   /api/draft/undo
GET    /api/draft/recommendations
GET    /api/draft/export.csv
```

All collection endpoints must support pagination, stable sorting, filters, and clear freshness
metadata. Use structured error codes in addition to human-readable corrective messages.

---

## 30. Expanded UI Quality Requirements

- Use a cohesive, original visual design appropriate for a dense draft-day tool.
- Keep primary actions and data legible at typical laptop widths without horizontal page scrolling.
- Dense tables may scroll within their panels and must preserve sticky headers.
- Meet WCAG 2.1 AA basics: keyboard navigation, focus indicators, labels, contrast, semantic tables,
  non-color status indicators, and reduced-motion support.
- Provide loading skeletons, empty states, stale-data banners, retry actions, and source-specific errors.
- Preserve filters, sort, selected league, queue, and layout preferences locally.
- Use optimistic UI only when a safe rollback is implemented.
- Avoid full-page reloads during draft activity.
- Support fast keyboard shortcuts for search, queue, draft/assign, undo, and position filters.
- Provide a command/help overlay listing shortcuts.
- Show exact source timestamps and a human-friendly age.
- Never show a derived value without a tooltip or detail explanation of its basis.

---

## 31. Expanded Tests

In addition to the existing test requirements, add:

### Source and identity tests

- Sleeper player-directory parsing and 24-hour caching;
- Sleeper trend parsing and attribution;
- nflverse dataset version/license metadata;
- provider failure leaves the last good snapshot intact;
- no unapproved FantasyPros scraping or network calls;
- exact ID crosswalk;
- ambiguous name match remains unresolved;
- manual identity override persists;
- team-defense and suffix normalization;
- source values preserve provenance.

### Consensus tests

- missing ranks are excluded rather than treated as zero;
- rank percentiles normalize different source pool sizes;
- weights change results predictably;
- median, best, worst, range, and source count are correct;
- incompatible values are not averaged directly;
- deterministic ties;
- a one-source sheet is labeled as one source, not broad consensus;
- user override wins only in the personalized view;
- tier generation and manual tier boundaries persist;
- high-disagreement flag is deterministic;
- separate leagues produce separate adjusted boards.

### Player and roster tests

- all MFL players remain visible when enrichment data is missing;
- combined position/availability/team/owner filters work;
- roster ownership always follows MFL;
- keeper and drafted players are unavailable;
- every franchise roster includes status and open slots;
- pagination and stable sort do not duplicate or omit players.

### Real draft tests

- add, edit, delete, and undo a legal real pick;
- prevent a duplicated drafted player;
- queue ordering persists;
- MFL reconciliation reports conflicts before applying them;
- MFL reconciliation is idempotent;
- manual picks are preserved when MFL is temporarily stale;
- recommendations respond to roster needs and tier depletion;
- hidden-tab polling pauses;
- no mock-draft routes or simulation behavior are introduced.

### Setup/security tests

- first-run state clearly identifies missing configuration;
- league connection test verifies ID, season, name, and host;
- API keys and passwords are never returned to the browser after storage;
- secrets are redacted from logs and error traces;
- commissioner operations remain disabled by default;
- client registration/User-Agent is not mistaken for authentication.

---

## 32. Expanded Acceptance Criteria

The full drafting website is complete only when all earlier acceptance criteria and all of the
following are true:

1. A user can configure and verify both MFL leagues from the website without manually editing code.
2. The dashboard shows both leagues and the freshness/health of every enabled source.
3. The All Players page displays the full relevant MFL player pool.
4. Position, NFL team, availability, owner, injury, rookie, tier, and value filters work together.
5. The League Rosters page shows every franchise and every owned player with roster status.
6. The cheat sheet combines available approved sources transparently and shows source count and
   disagreement.
7. Each league receives a separate scoring- and lineup-adjusted board.
8. Users can edit tiers, notes, targets, fades, exclusions, and a persistent queue.
9. The real draft room tracks actual picks, availability, recent picks, roster construction, tier
   depletion, and team needs.
10. Recommendations explain value, need, scarcity, and tier cliffs.
11. Keeper and auction workflows continue to satisfy their specialized requirements.
12. No mock-draft engine, simulated opponents, or bot drafting is included.
13. No proprietary site is scraped and no paid ranking data is redistributed without documented
    permission.
14. Every external value has source, timestamp, freshness, and attribution.
15. The site remains useful with MFL alone when all optional enrichment sources are disabled.
16. README setup and screenshots cover first run, all players, rosters, cheat sheet, draft room,
    keepers, auction, data sources, and exports.

---

## 33. Expanded Implementation Order

Continue from the current application in this order unless repository evidence justifies a change:

1. Audit the implemented application against this expanded specification.
2. Add the setup/connection UI and secure local credential storage.
3. Add source registry, snapshots, provenance, and source-health UI.
4. Complete the canonical player identity/crosswalk layer.
5. Build the complete All Players query, filters, pagination, and player detail.
6. Build league roster grid/table and franchise detail pages.
7. Add approved Sleeper metadata/trending integration.
8. Add approved nflverse identity, roster, history, and opportunity integrations.
9. Add CSV ranking/projection import with mapping preview.
10. Implement consensus normalization, weighting, disagreement, and snapshots.
11. Build the personalized cheat-sheet editor, tiers, notes, tags, and queue.
12. Add the real non-auction draft session, picks, MFL reconciliation, and undo.
13. Add explainable draft recommendations and tier/roster-need updates.
14. Integrate the existing keeper and auction rooms into the shared player/roster/consensus model.
15. Add exports, source attribution, security tests, accessibility checks, and responsive QA.
16. Run the complete Definition of Done and manually exercise both configured leagues.
