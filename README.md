# Yahoo Fantasy Tool — Backend (FastAPI)

A local-first FastAPI backend that connects to Yahoo Fantasy Sports (OAuth2), pulls your leagues/teams/rosters, and exposes tidy endpoints your React/TypeScript frontend can consume for analytics.

## Features

- Yahoo OAuth2 (read-only scope `fspt-r`)
- Secure OAuth flow with **state** validation (CSRF defense)
- **Encrypted** token storage (Fernet)
- Auto-refresh tokens on 401 and persist new token row
- League discovery & parsing across sports (NBA/NHL/MLB/NFL)
- Teams & roster endpoints (sport-agnostic parsers)
- Strict CORS (frontend whitelist)
- **No debug routes** in production (removed for security)

---

## Tech Stack

- **Python 3.12**, **FastAPI**, **Uvicorn**
- **SQLAlchemy** + **PostgreSQL** (Neon)
- **requests**, **requests-oauthlib**
- **pydantic-settings** for config
- **cryptography** (Fernet) for token encryption

---

## Directory Layout

Yahoo-Fantasy-BE/
├─ app/
│ ├─ api/
│ │ ├─ routes_auth.py
│ │ ├─ routes_me.py
│ │ └─ routes_league.py
│ ├─ core/
│ │ ├─ config.py
│ │ ├─ crypto.py
│ │ └─ security.py
│ ├─ db/
│ │ ├─ models.py
│ │ └─ session.py
│ ├─ services/
│ │ ├─ yahoo.py
│ │ ├─ yahoo_client.py
│ │ ├─ yahoo_oauth.py
│ │ ├─ yahoo_parsers.py
│ │ ├─ yahoo_profile.py
│ │ └─ yahoo_matchups.py
│ └─ main.py
├─ .env
├─ requirements.txt
├─ README.md
└─ .gitignore

yaml
Copy code

---

## Prerequisites

- Python 3.12
- Postgres (Neon recommended)
- Yahoo Developer App (Client ID/Secret, registered redirect URI)
- (Optional) **ngrok** for remote OAuth callback while developing

---

## Setup

1. **Clone & install**
   ```bash
   python -m venv .venv
   . .venv/bin/activate              # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
Create .env

env
Copy code
# App
APP_NAME=YahooFantasyAPI
APP_ENV=local
CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173","https://YOUR-NGROK-SUBDOMAIN.ngrok-free.app"]
SECRET_KEY=change_me_dev_only

# Must be a valid Fernet key (44-char urlsafe base64). Generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=REPLACE_WITH_FERNET_KEY

# Database (Neon example)
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require&channel_binding=require

# Yahoo OAuth
YAHOO_CLIENT_ID=...
YAHOO_CLIENT_SECRET=...
YAHOO_REDIRECT_URI=https://YOUR-NGROK-SUBDOMAIN.ngrok-free.app/auth/callback
YAHOO_AUTH_URL=https://api.login.yahoo.com/oauth2/request_auth
YAHOO_TOKEN_URL=https://api.login.yahoo.com/oauth2/get_token
YAHOO_API_BASE=https://fantasysports.yahooapis.com/fantasy/v2

# Toggle stub mode off in real usage
YAHOO_FAKE_MODE=false
Run the API

bash
Copy code
uvicorn app.main:app --host 127.0.0.1 --port 8000
Local docs: http://127.0.0.1:8000/docs

(Optional) ngrok for OAuth

bash
Copy code
ngrok http http://127.0.0.1:8000
Set YAHOO_REDIRECT_URI in .env and in the Yahoo Developer Console to:

arduino
Copy code
https://<your-ngrok>.ngrok-free.app/auth/callback
When using ngrok, start login via the ngrok domain:

arduino
Copy code
https://<your-ngrok>.ngrok-free.app/auth/login
OAuth Flow (Brief)
GET /auth/login

Sets an oauth_state cookie and redirects to Yahoo.

Yahoo redirects to GET /auth/callback?code=...&state=...

Verifies state from cookie.

Exchanges code for tokens.

Encrypts and stores tokens (access + refresh).

Persists user profile (GUID/nickname).

Redirects to /.

Common pitfalls

Invalid or missing OAuth state → You didn’t start from the same domain as the callback (use ngrok URL for both login + callback).

Fernet key errors → Ensure ENCRYPTION_KEY is a valid urlsafe base64 32-byte key (44 chars, ends with =).

API Endpoints
Until full auth is added, endpoints expect a user_id (Yahoo GUID) via query/header (X-User-Id).
You can discover your GUID by calling Yahoo’s /users;use_login=1 after login, or from the stored user record.

Health
GET /health → {"ok": true, "env": "local"}

OAuth
GET /auth/login → redirect to Yahoo

GET /auth/callback?code=...&state=... → token exchange & store

Me
GET /me/leagues

Query: sport, season, game_key

Returns: array of leagues with merged stat categories

GET /me/matchups

Query: league_id, week, include_points, include_categories, limit

Returns: parsed current/past week matchups (points + categories if requested)

GET /me/my-team

Query: league_id

Returns: your team info (by GUID) + teams list

League
GET /league/{league_id}/teams

Returns: teams in a league (with manager guid/nickname)

GET /league/team/{team_id}/roster

Query: date (YYYY-MM-DD)

Returns: team roster + positions

Security Hardening (Current)
✅ State verification in OAuth callback (CSRF defense)

✅ Encrypted token storage (cryptography.Fernet)

✅ Strict CORS (only trusted frontend origins)

✅ Removed /debug routes in production

🔜 Plan: replace manual user_id with real user sessions/JWT

Development Tips
GUID discovery: after login, you can call the Yahoo /users;use_login=1 endpoint through the backend to read your GUID from the stored profile or initial calls.

Token refresh: if access token expires, the backend will decrypt the refresh token, rotate tokens, and persist a new encrypted row.

Troubleshooting
400 “Invalid or missing OAuth state”

Start login at the same domain as YAHOO_REDIRECT_URI (e.g., ngrok URL for both).

Ensure your client preserves cookies and follows redirects.

“Fernet key must be 32 url-safe base64-encoded bytes.”

Regenerate a key:

bash
Copy code
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Paste into .env as ENCRYPTION_KEY=....

404 for /me/...

Don’t use a literal {{base_url}} placeholder. Use http://127.0.0.1:8000 or your ngrok URL.

CORS errors in browser

Add your frontend origin to CORS_ORIGINS list and restart.

Deployment (High-Level)
Set environment variables (no secrets in code).

Use a production Postgres (Neon).

Run with Uvicorn/Gunicorn (multiple workers), HTTPS termination in front (platform managed).

Make sure CORS_ORIGINS reflects your real frontend domains.

Keep /debug code out of the deployed build.

