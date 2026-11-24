# dr7-fantasy-data

NBA data for the dR7 Fantasy app. Pulled nightly on PC-SALEM and pushed here,
so the app, drapha-PC and Claude all read from one place.

## Contents

| Path | What it holds | Updated |
|---|---|---|
| `data/games/YYYY-MM-DD.json` | Every final game that date: per-player traditional + advanced + usage stats, team totals, starters/bench | Nightly, append-only |
| `data/injuries.json` | Current injuries by team, with status and return dates (ESPN) | Every run |
| `data/odds.json` | Spread and over/under for upcoming games (ESPN) | Every run |
| `data/logs.json` | Last 30 games per player this season, with position (G/F/C) and usage | Every run |
| `data/dvp.json` | Defence by position: for each team and G/F/C (plus `all`), how much opponents produced versus their own per-minute norm (1.08 = 8% more), rank 1 = toughest, and `beta`, the per-stat weight the app gives the matchup (re-fit nightly, walk-forward; `tools/dvp_fit.py` prints the backtest) | Every run |
| `data/meta.json` | Timestamp of the last run | Every run |
| `data/history/<season>_players.json.gz`, `_teams.json.gz` | Past regular seasons: every player game and team game (NBA.com LeagueGameLog), compact `cols` + `rows` | Once, with `--history` |

## Running it

    python pull.py              # last 3 days + injuries/odds, then push
    python pull.py --backfill   # whole season from SEASON_START
    python pull.py --days 7     # last 7 days
    python pull.py --no-push    # write files, skip git
    python pull.py --force      # rewrite dates already saved
    python pull.py --history 5  # one-off: last 5 past seasons into data/history/ (about 10 calls)

Dates already saved are skipped, so re-running is safe and cheap.

## Reading it from a Worker

Files are served raw, no token needed:

    https://raw.githubusercontent.com/drapha7/dr7-fantasy-data/main/data/injuries.json

## Sources

- Box scores: stats.nba.com via `nba_api`. Only responds to residential IPs,
  which is why this runs on PC-SALEM rather than in the cloud.
- Injuries and odds: ESPN's public endpoints. Undocumented, so they can change.
