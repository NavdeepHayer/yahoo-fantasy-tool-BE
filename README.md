# Yahoo Fantasy Analytics – Backend (FastAPI)

A FastAPI backend that connects to Yahoo Fantasy Sports (OAuth2), fetches leagues/teams/rosters, and exposes clean APIs for a React/TypeScript frontend.

## Features
- Yahoo OAuth2 (read-only)
- League discovery (with stat category enrichment from settings)
- Teams + roster endpoints
- Robust parsers for Yahoo’s JSON shapes (NBA/NHL)
- Handy debug endpoints

## Stack
Python 3.12 · FastAPI · Uvicorn · SQLAlchemy · requests-oauthlib

## Getting Started
1. `python -m venv .venv && source .venv/bin/activate` (Windows: `.\.venv\Scripts\Activate.ps1`)
2. `pip install -r requirements.txt`
3. Copy `.env.example` → `.env` and fill in Yahoo OAuth credentials.
4. `uvicorn app.main:app --host 127.0.0.1 --port 8001`
5. Visit `http://127.0.0.1:8001/docs` for API docs.

## Yahoo OAuth
- Create an app in Yahoo Developer Console with scope `fspt-r`.
- Redirect URI: `http://127.0.0.1:8001/auth/callback`
- Flow:
  - `GET /auth/login` → open auth URL
  - Sign in → redirected to `/auth/callback?code=...`
  - Tokens are persisted (auto-refresh handled).

## API (high level)
- `GET /health` → `{ ok: true, env: "local" }`
- `GET /me/leagues` → list leagues (filters: `sport`, `season`, `game_key`)
- `GET /league/{league_id}/teams` → list teams
- `GET /league/team/{team_id}/roster` → roster (optional `date=YYYY-MM-DD`)

Debug:
- `GET /debug/yahoo/raw?path=...`
- `GET /debug/me/games`
- `GET /debug/me/leagues`
- `GET /debug/parse/leagues-by-key?game_key=...`
- `GET /debug/parse/teams?league_id=...`

## Roadmap
- Player projections + streamer suggestions
- Matchup/category win probability
- Caching & pagination
- FE (React + TS) consuming these endpoints