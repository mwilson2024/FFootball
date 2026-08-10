# DraftDesk — MFL Fantasy Draft Manager

DraftDesk is a local-first FastAPI drafting website for a real MyFantasyLeague keeper league and
auction league. It includes the full MFL player pool, every roster, a transparent consensus cheat
sheet, real non-auction pick tracking, keeper planning, live auction budgets, and guarded exports.
It has no mock drafts, simulated opponents, paid rankings, or FantasyPros scraping.

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

## Website guide

- **Home:** both leagues, sync freshness, warning counts, and room shortcuts.
- **All Players:** all relevant MFL players with sortable headers, per-column filters, combined
  position/team/owner/availability/personal-tag filters, and server-side pagination.
- **League Rosters:** every franchise and player, slots, positional counts and needs, salaries,
  keeper status, and strength. Auction leagues also show remaining funds and maximum legal bid.
- **Cheat Sheet:** a per-league percentile blend of enabled rank sources plus the local scoring/VOR
  model. It displays source count, mean, median, best/worst, range, and disagreement. Import legally
  obtained CSV rankings using `player_name,team,position,overall_rank`.
- **Draft Room:** the actual non-auction draft, with manual picks, queue, recommendations, roster
  need, tier inventory, undo, MFL `draftResults` reconciliation preview, and recap export.
- **Keepers:** MFL-selected keepers and the available league board. Local choices remain distinct
  until an explicit export or submission.
- **Auction:** admin-controlled live status, shared three-second viewer updates, atomic purchases,
  correction/reassignment tools, strategy-aware dynamic pricing, undo/redo, CSV, MFL XML, and a
  commissioner-only MFL import that requires a fresh XML preview plus explicit confirmation.
- **Power Rankings:** a deterministic league-strength board based on legal starting-lineup value,
  bench depth, and unfilled lineup spots, with an optional on-demand ChatGPT league judgment.
- **Bye Advisor:** compare every active bye or choose one roster player manually, then see the best
  available same-position replacements using overall rank and the selected week's matchup.
- **Data Sources:** inspect shared cached sources and privately preview, include, exclude, or weight
  each one for your board: MFL, the attributed CC-BY-4.0
  GNG Pigskin board, FantasyPros ECR using your API key, free Sleeper metadata/trends, CC-BY-4.0
  nflverse identity, weekly depth chart, schedule, and historical player-stat data, and user CSV
  imports.
- **Scoring:** grouped MFL scoring rules with every repeated range retained, readable event names,
  normalized values, mapping state, and the raw imported response.
- **Settings:** configure both leagues without editing code and test public and protected access
  separately. The User-Agent identifies the app; it is not authentication.
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

The GNG rankings need no key and are attributed under CC BY 4.0. FantasyPros uses the official API
and stores its key in Windows Credential Manager. ADFL receives both redraft and dynasty ECR;
TMFL receives redraft ECR only. Sleeper trends and nflverse schedule difficulty use small ranking
weights, while their metadata also enriches player profiles. Source health records last attempt,
success, error, cache interval, license, and terms.
The site remains fully usable with MFL and the local league model alone.

Both configured leagues and every automatic source refresh daily at **1:00 AM America/New_York**.
The scheduler is part of the web service, honors daylight-saving time, attempts both leagues even
if one fails, and records source failures without stopping the remaining refreshes.

Synchronization warnings are intentionally not shown in the normal website. A rotating internal
diagnostic log is kept at `data/logs/sync_warnings.log` (2 MB per file, five backups) for debugging.

## Commissioner safety and exports

Commissioner writes are disabled unless `MFL_ENABLE_IMPORTS=true` and session/local commissioner
credentials are configured. Auction import requires an exact XML preview and unchanged SHA-256
confirmation token. The app fetches fresh MFL state before POSTing and never submits automatically.

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
   will be invalidated. Add optional MFL league keys and `FANTASYPROS_API_KEY` as Railway variables.
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
