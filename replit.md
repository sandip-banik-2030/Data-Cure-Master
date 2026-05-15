# Datacure

A mobile-first fintech rewards app where Indian users earn Datacure Coins by saving mobile data and watching ads, then redeem coins for mobile recharge packs.

## Run & Operate

- `python artifacts/datacure/run.py` — run the Datacure Flask app (port 5000)
- Workflow: **Datacure App** — starts and restarts the Flask server automatically
- `pnpm --filter @workspace/api-server run dev` — run the Node.js API server (port 8080)
- `pnpm run typecheck` — full typecheck across all packages
- Required env: `SESSION_SECRET` — Flask session secret key

## Stack

- **Backend**: Python 3.11 + Flask + SQLite3 (Datacure app)
- **Frontend**: Jinja2 HTML templates + vanilla CSS/JS (mobile-first)
- **Node.js infra**: pnpm workspaces, Express 5 API server (for platform use)

## Where Things Live

```
artifacts/datacure/
├── app.py                    # Main Flask app — all routes
├── run.py                    # Entry point (runs app.py)
├── datacure.db               # SQLite database (auto-created)
├── database/
│   ├── db.py                 # DB connection, init, migrations
│   └── schema.sql            # Table definitions
├── services/
│   ├── auth.py               # Phone encode/decode, operator detection, validators
│   ├── wallet.py             # Coin add/deduct — always server-side
│   ├── streak.py             # Daily streak tracking + bonus coins
│   └── ratelimit.py          # Sliding window rate limiter (5 req/5s)
├── templates/                # Jinja2 HTML pages
└── static/css/style.css      # Dark fintech UI (neon green + purple)
```

## Product

- **Register/Login** — Phone + password + simulated OTP (1234 for demo)
- **Log Data** — Submit MB saved → earn 5 coins per 100MB (server-side)
- **Watch Ads** — 15-second simulated ad timer → +10 coins each, max 15/day
- **Daily Streak** — +20 coins per consecutive login day; 7-day milestone
- **Redeem** — ₹15 (1500 coins) or ₹20 (2000 coins) mobile recharge requests
- **My Requests** — Full history of recharge redemptions with status
- **Admin Panel** — `/admin` (requires `is_admin=1` in DB) for managing requests

## Architecture Decisions

- Coins ONLY modified via `wallet.py` `add_coins`/`deduct_coins` — never raw SQL from routes
- Phone numbers stored base64-encoded in DB (not plaintext)
- All coin math is server-side; frontend values never trusted
- Sliding-window rate limiting per (endpoint, user/IP) stored in Flask session
- `init_db()` runs on startup and is idempotent — safe to restart without data loss

## User Preferences

- Dark futuristic fintech UI: neon green (#00ff88) + purple (#a855f7) on dark (#080810)
- Mobile-first layout with bottom navigation bar
- OTP fixed at 1234 for demo/simulation

## Gotchas

- Flask app must be run from workspace root (`python artifacts/datacure/run.py`) so imports resolve correctly
- DB file lives at `artifacts/datacure/datacure.db` — never delete this during development
- `streak_bonus_date` and other columns are auto-migrated on startup via `_migrate()` in `db.py`
- Admin access requires manually setting `is_admin=1` in the `users` table

## Pointers

- See `artifacts/datacure/database/schema.sql` for the full DB schema
- See `.local/skills/pnpm-workspace` for workspace structure details
