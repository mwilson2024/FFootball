# DraftDesk — MFL Fantasy Draft Manager

DraftDesk is a local-first FastAPI drafting website for a real MyFantasyLeague keeper league and
auction league. It includes the full MFL player pool, every roster, a transparent consensus cheat
sheet, real non-auction pick tracking, keeper planning, live auction budgets, and guarded exports.
It includes an admin-enabled shared practice draft, but no simulated opponents, ranking-site
scraping, or limited ranking API dependency.

## Start the website

Requires Python 3.11+ (3.12 recommended). From PowerShell in `C:\FFootball`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Go to **Settings**, enter the season and both
league IDs, test each connection, save, then choose **Synchronize MFL**. Public leagues normally do
not need a key. Optional league API keys are saved in Windows Credential Manager and are never
returned to the browser after saving. Environment variables in `.env` remain supported and take
precedence.

### Convert the ranking PDFs again

The repository includes a reusable converter for the two PDFs under `PDF/`. It validates that the
PPR sheet contains ranks 1-300 and the dynasty sheet contains ranks 1-240 before replacing either
CSV. From PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\convert_ranking_pdfs.py
```

The generated files are `CSV/NFL26_CS_PPR300.csv` and
`CSV/espnDynastyNFL26_CS_Dyn.csv`. Those two files plus the FantasyPros redraft, FantasyPros dynasty,
FantasySharks dynasty, and PFF fantasy ranking CSVs are loaded automatically at startup, after an
MFL synchronization, and during the 1:00 AM daily refresh. Repository CSVs are shared with every
signed-in account. A CSV
imported through the Cheat Sheet belongs only to the account that uploaded it.

## Website guide

- **Home:** both leagues, sync freshness, warning counts, and room shortcuts.
- **All Players:** all relevant MFL players with sortable headers, per-column filters, combined
  position/team/owner/availability/personal-tag filters, and server-side pagination.
- **League Rosters:** every franchise and player, slots, positional counts and needs, salaries,
  keeper status, and strength. Auction leagues also show remaining funds and maximum legal bid.
- **Cheat Sheet:** a per-league percentile blend of enabled rank sources, with an optional local
  scoring/VOR model that defaults off. It displays source count, mean, median, best/worst, range,
  and disagreement. Compare two to
  five players side by side, use a random tie-breaker, or ask ChatGPT to choose from server-verified
  league values. Import legally obtained CSV rankings using
  `player_name,team,position,overall_rank`.
- **Draft Room:** the admin can keep player selections locked, choose MFL companion mode or a
  real-time local draft, and open one shared mock room that follows MFL's order while storing
  practice picks separately. Companion mode imports MFL picks every 30 seconds. Local mode lets the
  team on the clock select in DraftDesk and broadcasts the result to every connected screen without
  sending it to MFL. Changing the real-draft method while live pauses the room first.
  The room includes manual picks, queue, recommendations, roster need, a personal war room,
  live position-run/tier-cliff/value intelligence,
  next-pick survival heuristics, and an expandable Draft Scenario Lab for every recommendation.
  The lab shows the expected final roster strength, next-pick survival, likely alternatives,
  position-cliff pressure, bye/lineup effects, value versus waiting, and model confidence with its
  contributing sources. Projected standings and the read-only “what if” replay live separately
  under Power Rankings so the live Draft Room does not rebuild league-wide analysis on each update.
  Nearby opponent needs, full opposing-owner roster/need/tendency
  profiles, undo, MFL `draftResults` reconciliation
  preview, recap export, and a full-screen live board that switches between team columns and
  chronological pick order. During a real live draft, admins can correct the player, franchise,
  round, or pick number on locally recorded selections, or remove a bad selection entirely.
- **Keepers:** MFL-selected keepers and the available league board. Local choices remain distinct
  until an explicit export or submission.
- **Auction:** three admin-controlled states: closed (everyone read-only), staging (admins can record
  local test/preparation purchases), and live (purchase access follows Rob mode). Admins can
  optionally replace manual entry with the shared nomination and bidding room. Only the signed-in
  MFL owner whose team is currently up may nominate; connected owners bid for their own franchise,
  the room shows the nominated player's photo and live high bid, and an admin awards the winner.
  The room stays hidden while this option is off. The auction also has server-pushed viewer
  updates, atomic purchases, correction/reassignment tools, strategy-aware
  dynamic pricing, live market intelligence, and a personal auction war room with roster needs,
  affordable targets, bye conflicts, and opposing-owner budget and bidding profiles. It also
  includes undo/redo, CSV, MFL XML, and a commissioner-only MFL import that requires a fresh XML
  preview plus explicit confirmation.
- **Power Rankings:** a deterministic league-strength board based on legal starting-lineup value,
  bench depth, and unfilled lineup spots. Its post-draft lab adds projected standings, position
  grades, steals/reaches, roster weaknesses, and a read-only counterfactual replay, plus an optional
  on-demand ChatGPT league judgment.
- **Bye Advisor:** compare every active bye or choose one roster player manually, then see the best
  available same-position replacements using overall rank and the selected week's matchup.
- **Data Sources:** inspect every source in a printable, downloadable spreadsheet and privately
  preview, include, exclude, or weight each one for your board: MFL, the attributed CC-BY-4.0
  GNG Pigskin board, six full shared ranking CSVs, free Sleeper metadata/trends, CC-BY-4.0 nflverse
  identity, weekly depth chart, schedule, and historical player-stat data, and private user CSV
  imports.
- **Links:** open the ranking pages saved under `Links/Links.txt` and a curated set of current draft,
  auction, dynasty, injury, sleeper, fade, and official NFL calendar resources.
- **Scoring:** grouped MFL scoring rules with every repeated range retained, readable event names,
  normalized values, mapping state, and the raw imported response.
- **Settings:** configure both leagues without editing code and test public and protected access
  separately. Admins can also see which previously signed-in users are online or recently active.
  The User-Agent identifies the app; it is not authentication.
- **My Account:** select your franchise and auction strategy. Player tiers, targets, sleepers,
  queues, and source adjustments belong only to the signed-in MFL user.
- **League assistant:** optional OpenAI-powered advice using the selected user franchise, roster,
  budget, scoring, and lineup context. It cannot submit bids or picks.

Use `/` to focus the current page search and `?` to show keyboard help. Dense tables retain sticky
headers and scroll inside their panels.

## Data and ranking honesty

MFL remains authoritative for ownership. Every league gets a separate scoring- and lineup-adjusted
VOR board. MFL ADP and AAV are market references; a weekly MFL projection is never mislabeled as a
season projection. Missing values are ignored rather than treated as zero. “Consensus” means the
configured local blend and never implies FantasyPros Expert Consensus Rank.

The GNG rankings need no key and are attributed under CC BY 4.0. ADFL receives the shared ESPN,
FantasyPros, and FantasySharks dynasty CSVs; TMFL receives the shared ESPN PPR Top 300 and
FantasyPros redraft CSV. Both leagues receive the shared PFF 2026 fantasy ranking CSV.
The limited live FantasyPros API and API-key controls remain removed; FantasyPros data now comes
from the complete uploaded files and is attributed with its filename. Sleeper trends and nflverse
schedule difficulty use small ranking weights, while their metadata also enriches player profiles.
Source health records last attempt, success, error, cache interval, license, terms, and whether the
source is shared or private.
The league scoring/VOR model defaults off so the uploaded and synchronized rankings lead the board;
each user may turn the model on and choose its influence from Ranking Sources.

Both configured leagues and every automatic source refresh daily at **1:00 AM America/New_York**.
The scheduler is part of the web service, honors daylight-saving time, attempts both leagues even
if one fails, and records source failures without stopping the remaining refreshes.

Synchronization warnings are intentionally not shown in the normal website. A rotating internal
diagnostic log is kept at `data/logs/sync_warnings.log` (2 MB per file, five backups) for debugging.

## Commissioner safety and exports

Commissioner writes require the persistent **Commissioner imports** switch in Admin plus configured
commissioner credentials. `MFL_ENABLE_IMPORTS` supplies the initial default when no saved Admin choice
exists. Auction import requires an exact XML preview and unchanged SHA-256 confirmation token. The app
fetches fresh MFL state before POSTing and never submits automatically.

Real draft picks are rejected until an admin starts the real draft. Companion mode always rejects
local real picks; local mode accepts them only from an admin or the MFL team currently on the clock.
Starting shared mock mode pauses the real draft, and starting the real draft ends mock mode. Mock
picks use a separate database table, reject stale simultaneous selections, and are never included in
MFL reconciliation or commissioner imports. Closing auction staging also ends the live auction and
locks new purchases.

Auction CSV/XML, checksums, manifests, and draft recaps are written atomically to `exports/`. Ranking
imports and checksums are retained under `data/imports/`. IDs remain strings so leading zeroes are
preserved.

Every local draft-pick and auction-purchase create, correction, removal, undo, redo, reconciliation,
or confirmed MFL import is also appended to a separate hash-chained JSONL backup under `data/audit/`.
These records include before/after state and actor, contain no credentials, and are kept outside the
SQLite transaction history so a later correction does not erase the original record.

## Quality checks

```powershell
ruff check app tests
ruff format --check app tests
mypy app
pytest -q
```

Tests use local fixtures and mocked transports; they do not call the live MFL API.

## Deploy on Railway

Railway installs production dependencies from the committed `requirements.txt`; `railway.toml`
also runs that installation explicitly for deterministic Railpack builds. If deployment logs say
`No module named uvicorn`, confirm both files are present in the deployed GitHub commit.

1. Push this project to a private GitHub repository and create a Railway service from that repo.
   Railway reads `railway.toml`, starts Uvicorn on the provided `PORT`, and checks `/health` before
   directing traffic to a new deployment.
2. Add a Railway volume mounted at `/app/data`. Keep the service at **one replica** because SQLite
   is a single-file database. Use Railway Postgres before scaling horizontally.
3. Add these service variables (do not commit their values):

   ```text
   APP_ENV=production
   AUTH_REQUIRED=true
   SESSION_SECRET=<a stable random value of at least 32 characters>
   DATABASE_URL=sqlite:////app/data/fantasy_draft.db
   EXPORT_DIRECTORY=/app/data/exports
   AUDIT_DIRECTORY=/app/data/audit
   MFL_SEASON=2026
   MFL_KEEPER_LEAGUE_ID=<ADFL league id>
   MFL_AUCTION_LEAGUE_ID=<TMFL league id>
   ALLOWED_HOSTS=*.up.railway.app,healthcheck.railway.app
   AUTO_SYNC_ENABLED=true
   AUTO_SYNC_TIMEZONE=America/New_York
   AUTO_SYNC_HOUR=1
   ADMIN_USERNAMES=wilsonmw
   ```

   Generate `SESSION_SECRET` locally with `python -c "import secrets;
   print(secrets.token_urlsafe(48))"`. Keep it unchanged across deployments or every active login
   will be invalidated. Add optional MFL league keys as Railway variables.
   `wilsonmw` is the initial administrator; add more comma-separated MFL usernames to
   `ADMIN_USERNAMES`. To enable the optional league assistant, also add `OPENAI_API_KEY` and,
   optionally, `OPENAI_MODEL` (defaults to `gpt-5.6`). Source/player data stays in DraftDesk; only a
   bounded league context and the user question are sent when the assistant is explicitly used.
4. Generate a Railway domain, open the HTTPS URL, and sign in with an MFL account that belongs to
   both configured leagues. MFL credentials are verified directly with MFL and are not retained by
   DraftDesk.

The `/app/data` volume holds the SQLite database, generated exports, and append-only audit backups.
Railway redeploys can replace application files, so do not mount the volume over `/app`; mount it at
exactly `/app/data` and include it in your normal Railway volume backup routine.

The production cookie is HTTP-only, secure, SameSite=Lax, HMAC-signed, and valid for 30 days.
Mutating requests require a CSRF token; login attempts are rate-limited; host validation and common
  browser security headers are enabled. Railway must use HTTPS (its generated domains do by default).

## Season projection model and live delivery

DraftDesk's `season-outcomes-v1` model is intentionally separate from the ranking blend. When an
imported source contains a real full-season projection, the model uses it. Otherwise it recalculates
the latest nflverse regular-season stat line with the selected league's imported linear MFL scoring
rules, then regresses that result toward the player's current role. The UI reports median, ceiling,
floor, workload, injury risk, confidence, source names, and the exact basis. Weekly MFL projections
and rank-derived fallbacks are never relabeled as vendor season projections. Flat weekly threshold
bonuses are excluded from season-total recalculation because aggregate stats cannot reveal how many
individual weeks crossed the threshold.

The server remains the only process that polls MFL during a live companion draft (once every 30
seconds). It publishes applied picks and local draft/auction mutations through a same-origin
server-sent event stream. Connected Draft Rooms, live boards, and Auction Rooms refresh from that
event; a slower fallback refresh remains in place if a proxy or browser cannot keep an event stream
open. The in-memory event broker assumes the documented one-replica SQLite Railway deployment. Use
a shared broker such as Redis together with Postgres before running multiple replicas.
