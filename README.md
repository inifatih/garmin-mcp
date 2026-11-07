# garmin-mcp

A private, single-user MCP server that gives Claude read-only access to **my own** Garmin Connect
data. It runs on Render's free tier and is added to claude.ai as a custom connector.

- **Access control:** the MCP endpoint lives at a secret URL path, `/<MCP_PATH_SECRET>/mcp`.
  Every other path returns 404, except `GET /health`.
- **Garmin tokens:** stored Fernet-encrypted in a secret GitHub gist. The server writes them back
  whenever Garmin rotates them, so restarts on Render's ephemeral disk don't break anything.
- **Credentials:** your Garmin email and password never leave your laptop.

## Tools

| Tool | Returns |
|---|---|
| `list_activities(start_date, end_date, activity_type="running", limit=30)` | id, name, type, start time, km, duration, pace, avg/max HR, elevation, cadence, training effect, VO2max |
| `get_activity(activity_id)` | summary + per-lap/km splits (pace, HR, cadence, elevation) + HR-zone time |
| `get_daily_summary(date)` | steps, resting HR, stress, Body Battery, calories, intensity minutes |
| `get_sleep(date)` | duration, stages, score, bed/wake time, overnight HRV, resting HR |
| `get_hrv(date)` · `get_training_readiness(date)` · `get_training_status(date)` · `get_max_metrics(date)` | recovery and fitness status, VO2max |
| `get_race_predictions()` · `get_personal_records()` · `get_endurance_score(date)` · `get_lactate_threshold()` | performance markers |
| `get_body_battery(start_date, end_date)` · `get_heart_rates(date)` | daily Body Battery, hourly HR summary |

Dates are `YYYY-MM-DD` and default to today in Asia/Jakarta.

## Setup

### 1. Create the tokens (once, on your laptop)

Requires Python 3.12. On Windows PowerShell:

```powershell
git clone https://github.com/inifatih/garmin-mcp.git
cd garmin-mcp
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python scripts\setup_tokens.py
```

The script will:
1. Ask for your Garmin email, password, and an MFA code if Garmin sends one. None of these are saved.
2. Ask for a GitHub **classic** token with **only** the `gist` scope. The script prints the link
   to create one.
3. Create a secret gist containing the encrypted tokens and print **4 env vars** plus your
   connector URL.

### 2. Deploy on Render

1. Render dashboard → **New → Blueprint** → pick `inifatih/garmin-mcp` (branch `main`).
2. When prompted, paste the 4 values: `GARMIN_TOKEN_KEY`, `GIST_ID`, `GIST_TOKEN`,
   `MCP_PATH_SECRET`.
3. Deploy. Check that `https://<service>.onrender.com/health` returns `ok`.

### 3. Add it to Claude

claude.ai → **Settings → Connectors → Add custom connector** → URL:

```
https://<service>.onrender.com/<MCP_PATH_SECRET>/mcp
```

Leave the OAuth fields empty. Then ask Claude something like "How did my runs go this week?"

## Good to know

- **The free tier sleeps.** Render spins the service down after about 15 minutes idle. The first
  call afterwards can take up to a minute while it wakes up and logs in to Garmin. If Claude
  times out, just ask again.
- **Tokens expired?** If tools say "run `setup_tokens.py --rotate`", run
  `python scripts\setup_tokens.py --rotate` and paste the existing `GIST_ID`, `GIST_TOKEN` and
  `GARMIN_TOKEN_KEY` when asked. The script logs in again and overwrites the gist. Then restart
  the Render service. No env vars change.
- **To revoke access:**
  - **Garmin access:** delete the gist (gist.github.com), or log out all sessions in Garmin
    Connect account settings.
  - **Gist access:** delete or regenerate the `gist`-scope token in GitHub → Settings →
    Developer settings.
  - **Connector URL:** set a new `MCP_PATH_SECRET` on Render, and update the URL in claude.ai.
- **The URL is the password.** Anyone with the connector URL can read your Garmin data. Don't
  share it. The server never logs the secret path or any tokens.
- **Local dev:** set `MCP_PATH_SECRET` and either the 3 gist vars or `GARMINTOKENS` (plaintext
  token JSON), then run `python server.py` (port from `PORT`, default 8000).

## Tests

```bash
pip install -r requirements.txt pytest
pytest
```

Garmin and the GitHub API are mocked. The tests cover token rotation → gist PATCH, the 404s for
wrong paths, `/health`, log redaction, and every tool's output trimming.
