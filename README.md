# 🏀 Yahoo Fantasy Analytics – Backend (FastAPI)

A **FastAPI** backend that connects to **Yahoo Fantasy Sports (OAuth2)**, fetches your leagues, teams, rosters, and matchups, and exposes clean, normalized APIs for a modern React/TypeScript frontend.  
Currently supports **NBA** and **NHL**, with flexible parsing for other Yahoo Fantasy sports.

---

## 🚀 Features

- ✅ **Yahoo OAuth2 (read-only)** — secure authentication and token refresh handling  
- 🏆 **League discovery** — returns all user leagues with season, sport, and scoring categories  
- 👥 **Teams & rosters** — clean, flattened structure across all Yahoo JSON shapes  
- 📊 **Matchups & weekly results** — points + category-level breakdown for each matchup  
- 🧠 **Smart data normalization** — auto-detects and parses all Yahoo Fantasy object variants  
- 🧰 **Robust debug tools** — `/debug/yahoo/raw` for inspecting raw Yahoo payloads  
- ⚙️ **Cross-sport compatible** — supports NHL/NBA JSON layouts (more coming)

---

## 🧩 Stack

**Python 3.12** · **FastAPI** · **Uvicorn** · **SQLAlchemy** · **requests-oauthlib**  
Optional: PostgreSQL (for persistent storage), but local in-memory works too.

---

## 🛠 Getting Started

```bash
# 1️⃣ Create virtual environment
python -m venv .venv
source .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1

# 2️⃣ Install dependencies
pip install -r requirements.txt

# 3️⃣ Set up environment variables
cp .env.example .env
# Fill in your Yahoo OAuth credentials

# 4️⃣ Start the server
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 5️⃣ Open interactive docs
http://127.0.0.1:8000/docs

🔐 Yahoo OAuth Setup

Go to Yahoo Developer Console
.

Create a new app with scope:

fspt-r


Set Redirect URI to:

http://127.0.0.1:8000/auth/callback


Flow:

GET /auth/login → opens Yahoo login

Redirected to /auth/callback?code=...

Tokens are saved automatically; refresh handled transparently.

🧠 API Endpoints Overview
Endpoint	Description	Example
GET /health	Health check	{ "ok": true, "env": "local" }
GET /me/leagues	Lists user’s leagues with filters (sport, season, game_key)	/me/leagues?sport=nhl&season=2025
GET /me/my-team	Returns your own team info (GUID, team_id, and all league teams)	/me/my-team?league_id=465.l.34067
GET /me/matchups	Returns your weekly matchup with category and point breakdown	/me/matchups?league_id=465.l.34067&week=1&include_points=true&include_categories=true
GET /league/{league_id}/teams	Lists all teams for a league	/league/465.l.34067/teams
GET /league/team/{team_id}/roster	Fetches roster for a given date	/league/team/465.l.34067.t.11/roster?date=2025-10-10
GET /debug/yahoo/raw?path=...	Raw Yahoo API passthrough for inspection	/debug/yahoo/raw?path=/league/465.l.34067/scoreboard;week=1
GET /debug/me/games	Lists all Yahoo games tied to your account	
GET /debug/me/leagues	Lists all leagues with raw Yahoo output	
🧾 Example Response – /me/matchups
{
  "user_id": "local-dev",
  "week": 1,
  "items": [
    {
      "league_id": "465.l.34067",
      "week": 1,
      "start_date": "2025-10-07",
      "end_date": "2025-10-12",
      "team_id": "465.l.34067.t.11",
      "team_name": "Tkachuk Norris",
      "opponent_team_id": "465.l.34067.t.1",
      "opponent_team_name": "Hughes Your Daddy",
      "status": "midevent",
      "is_playoffs": false,
      "score": {
        "points": { "me": "7", "opp": "2" },
        "categories": { "wins": 7, "losses": 2, "ties": 1 },
        "category_breakdown": [
          { "name": "G", "me": "2", "opp": "6", "leader": 2 },
          { "name": "A", "me": "8", "opp": "8", "leader": 0 },
          { "name": "+/-", "me": "2", "opp": "-4", "leader": 1 },
          ...
        ]
      }
    }
  ]
}

🧪 Postman Setup

A ready-to-import Postman collection is available:

Yahoo Fantasy Tool (BE) – Me.postman_collection.json


Or manually add these endpoints under your existing collection:

/me/my-team
→ Fetches your GUID, your team, and all teams in the league.

/me/matchups
→ Returns your current or specified week’s matchup including points + categories.

Environment Variable:

{{base_url}} = http://127.0.0.1:8000

🧩 Internal Architecture
app/
├── api/
│   ├── routes_me.py          # /me endpoints (leagues, my-team, matchups)
│   └── routes_debug.py       # /debug endpoints
├── services/
│   ├── yahoo.py              # Core Yahoo service and helpers
│   ├── yahoo_matchups.py     # Matchup parsing and normalization
│   ├── yahoo_client.py       # Handles OAuth and Yahoo API requests
├── db/
│   ├── session.py            # DB session and dependency
│   ├── models.py             # Optional persistent models
│   └── config.py             # DB and environment setup
├── main.py                   # FastAPI app entrypoint

🧭 Roadmap

Next:

 🧮 Add stat-name mapping for categories (G → Goals, etc.)

 🕹 NBA integration testing once games start

 🗓 Weekly matchup caching (PostgreSQL + scheduler)

 ⚡ API response caching layer (Redis / Memory)

 🎯 Predictive features (category win probability)

 📈 Player projections + waiver/streamer recommendations