# garmin-mcp — personal Garmin coach for Claude

A personal [MCP](https://modelcontextprotocol.io) server. It gives Claude live access to your
Garmin Connect data together with your training plan (**Sub-2:00 HM · JRF 2027**), so Claude can
coach you from real numbers: what to run today, whether to back off, how last week went, and
whether you're on track for sub-2:00.

It reads activities, HR time series, sleep, HRV, training readiness, training status and race
predictions. It can also **push structured, HR-targeted workouts to your watch**.

> Uses the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library
> (Garmin's official API is for approved businesses only). It logs in as you; your data stays
> between your machine, Garmin, and Claude.

## What Claude can do with it

| Tool | What it answers |
|---|---|
| `get_today` | "What do I run today?": planned session with HR targets and estimated time, what's already logged, readiness verdict, tomorrow |
| `review_week` | Sunday review: planned vs actual per day, km, and the plan's 6-item checklist (5/6 = good week) |
| `check_fatigue` | The plan's fatigue rules: easy-run HR drift at the same pace, bad-sleep streaks, HRV, readiness, ACWR → `proceed` / `easy_today` / `reduce_week_25` |
| `analyze_activity` | One run vs the plan: laps, time in **your** zones, % at HR ≤ 150, aerobic decoupling (Pa:HR), tempo block, strides |
| `analyze_test` | Time trials (best effort, pass/target/stretch, Riegel HM equivalent, race pace from 10K TT), monthly aerobic test (30 min at HR 143–147), tempo test |
| `get_progress` | Every test so far vs milestones, labelled improving / slowing / flat / regressing, plus 8-week volume |
| `get_race_outlook` | Days to JRF 2027, gap to 2:00, Garmin predictions, race-pace table, race execution |
| `get_athlete_snapshot`, `get_wellness_trend`, `get_training_status`, `list_activities` | Raw live data |
| `get_plan_overview`, `get_planned_week` | The plan itself (works without Garmin login) |
| `push_workout`, `push_week`, `push_custom_workout` | Upload structured workouts (warm-up / repeats / HR targets) to Garmin Connect and schedule them on your calendar. Each one previews first; it uploads only with `confirm=true` |

Prompts: `daily_checkin`, `weekly_review_prompt`, `monthly_progress_prompt`. Resource: `plan://current`.

## Setup (once, on your computer)

Requires Python 3.12+. [uv](https://docs.astral.sh/uv/) is the easiest way.

```bash
git clone https://github.com/inifatih/garmin-mcp.git
cd garmin-mcp
uv sync                       # or: python -m venv .venv && . .venv/bin/activate && pip install -e .
uv run garmin-mcp-login       # email, password, MFA code → tokens saved in ~/.garminconnect
```

You only log in once. The tokens refresh themselves, and your password is never stored.

## Connect it to Claude

### Claude Desktop (simplest)

Settings → Developer → Edit Config, then add (use the absolute path to the repo):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/garmin-mcp", "run", "garmin-mcp"]
    }
  }
}
```

Restart Claude Desktop, then ask: *"What's my run today?"*

### Claude Code

```bash
claude mcp add garmin -- uv --directory /ABSOLUTE/PATH/TO/garmin-mcp run garmin-mcp
```

### claude.ai and the phone app (remote server)

The web and mobile apps need the server reachable over HTTPS. Run it in HTTP mode behind a
secret token:

```bash
export GARMIN_MCP_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
uv run garmin-mcp --transport http --host 0.0.0.0 --port 8000
```

Expose it with any HTTPS tunnel or host, for example
`cloudflared tunnel --url http://localhost:8000`, or the Dockerfile on Fly.io, Render or a VPS. Then in
claude.ai go to **Settings → Connectors → Add custom connector** and use:

```
https://YOUR-HOST/mcp?token=YOUR_GARMIN_MCP_TOKEN
```

On a host without a filesystem for `~/.garminconnect`, run `garmin-mcp-login --print-tokens` on
your computer and put the printed JSON in the `GARMINTOKENS` secret. Garmin sometimes blocks
logins from cloud IPs, which is why you log in at home and copy only the tokens. The server
refuses to listen on a public interface without `GARMIN_MCP_TOKEN`.

```bash
docker build -t garmin-mcp .
docker run -p 8000:8000 -e GARMIN_MCP_TOKEN=... -e GARMINTOKENS='{"...":"..."}' garmin-mcp
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `GARMINTOKENS` | `~/.garminconnect` | Token directory, or the token JSON itself |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | – | Optional fallback login (no MFA) |
| `GARMIN_MCP_PLAN` | bundled `jrf2027_sub2.yaml` | Path to your own plan YAML |
| `GARMIN_MCP_RACE_PACE` | – | e.g. `5:39`. Set after TT 19 Sep so HM-pace steps get a pace target on the watch |
| `GARMIN_MCP_TOKEN` | – | Shared secret for HTTP mode (`Authorization: Bearer …` or `?token=…`) |
| `GARMIN_MCP_ALLOWED_HOSTS` | – | Comma-separated hostnames for DNS-rebinding protection in HTTP mode |
| `GARMIN_MCP_CACHE_TTL` | `120` | Seconds to cache Garmin responses within a conversation |

## The plan file

The plan is plain YAML at `src/garmin_mcp/plans/jrf2027_sub2.yaml`: goal, HR zones, phases,
milestones, test days, the weekly checklist, every rule from the plan doc, and all 51 weeks. Sessions
use a small notation that the coach tools and the workout builder both read:

```
2km easy · 13km steady
4km easy · 2x(3km tempo / 1km steady) · 4km easy     # repeats skip the last recovery
40min easy · 6x(20s strides / 60s easy)
```

Zones: `recovery`, `easy` (138–150), `aerobic_test` (143–147), `steady` (148–155), `tempo` (158–165),
`tempo_test` (160–165), `hmp`, `tt`, `race`, `strides`. When the plan changes, edit the YAML and
the server picks it up on the next call.

## Development

```bash
uv sync --extra dev   # or pip install -e ".[dev]"
pytest
```

The tests run against a fake Garmin client with synthetic HR and speed data, so they need no account.
