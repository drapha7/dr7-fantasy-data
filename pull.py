"""
dR7 Fantasy - NBA data pull for PC-SALEM.

Pulls finished-game box scores from stats.nba.com (via nba_api) and
injuries/odds from ESPN, writes them into data/ as JSON, then commits
and pushes to the GitHub data repo.

Usage
-----
  python pull.py                 # incremental: last 3 days + injuries/odds
  python pull.py --backfill      # whole season from SEASON_START
  python pull.py --days 7        # last 7 days
  python pull.py --no-push       # write files, skip git

Run from inside the cloned repo folder.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta

import requests

from nba_api.stats.endpoints import (
    ScoreboardV2,
    BoxScoreTraditionalV3,
    BoxScoreAdvancedV3,
    BoxScoreUsageV3,
)

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
SEASON_START = date(2026, 10, 1)   # scan starts here; empty dates are skipped
REQUEST_PAUSE = 1.0                # seconds between stats.nba.com calls
TIMEOUT = 60                       # per-request timeout, seconds
MAX_RETRIES = 3
STATUS_FINAL = 3                   # GAME_STATUS_ID for a completed game

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
GAMES_DIR = os.path.join(DATA, "games")
SEASON_DIR = os.path.join(DATA, "season")
LOG_PATH = os.path.join(ROOT, "pull.log")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def retry(fn, what):
    """Call fn() with backoff. Returns None if every attempt fails."""
    delay = 5
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == MAX_RETRIES:
                log(f"  FAILED {what}: {exc}")
                return None
            log(f"  retry {attempt}/{MAX_RETRIES} for {what} in {delay}s ({exc})")
            time.sleep(delay)
            delay *= 3
    return None


def rows(endpoint, dataset_name):
    """Return a list of dicts for one dataset of an nba_api endpoint."""
    if endpoint is None:
        return []
    try:
        normalized = endpoint.get_normalized_dict()
    except Exception as exc:
        log(f"  could not read {dataset_name}: {exc}")
        return []
    for key, value in normalized.items():
        if key.lower() == dataset_name.lower():
            return value or []
    return []


# ----------------------------------------------------------------------
# stats.nba.com
# ----------------------------------------------------------------------
def games_on(day):
    """Finished games for a date. Returns list of (game_id, matchup_label)."""
    sb = retry(
        lambda: ScoreboardV2(game_date=day.strftime("%Y-%m-%d"), timeout=TIMEOUT),
        f"scoreboard {day}",
    )
    time.sleep(REQUEST_PAUSE)
    if sb is None:
        return None  # distinguish "call failed" from "no games"

    out = []
    seen = set()
    for g in rows(sb, "GameHeader"):
        if g.get("GAME_STATUS_ID") == STATUS_FINAL and g["GAME_ID"] not in seen:
            seen.add(g["GAME_ID"])  # the scoreboard sometimes lists a game twice
            out.append((g["GAME_ID"], g.get("GAMECODE", "")))
    return out


def box_score(game_id):
    """All three box score views for one game, merged per player."""
    trad = retry(lambda: BoxScoreTraditionalV3(game_id=game_id, timeout=TIMEOUT),
                 f"traditional {game_id}")
    time.sleep(REQUEST_PAUSE)
    if trad is None:
        return None

    adv = retry(lambda: BoxScoreAdvancedV3(game_id=game_id, timeout=TIMEOUT),
                f"advanced {game_id}")
    time.sleep(REQUEST_PAUSE)

    use = retry(lambda: BoxScoreUsageV3(game_id=game_id, timeout=TIMEOUT),
                f"usage {game_id}")
    time.sleep(REQUEST_PAUSE)

    players = {}
    for r in rows(trad, "PlayerStats"):
        players[r["personId"]] = dict(r)

    for label, ep in (("adv", adv), ("usg", use)):
        if ep is None:
            continue
        for r in rows(ep, "PlayerStats"):
            pid = r.get("personId")
            if pid not in players:
                continue
            for k, v in r.items():
                # keep identity columns from traditional, add the new metrics
                if k not in players[pid]:
                    players[pid][k] = v

    return {
        "gameId": game_id,
        "players": list(players.values()),
        "teams": rows(trad, "TeamStats"),
        "teamsAdvanced": rows(adv, "TeamStats") if adv else [],
        "starterBench": rows(trad, "TeamStarterBenchStats"),
    }


def pull_date(day, force=False):
    """Write data/games/YYYY-MM-DD.json for one date. Returns True if written."""
    path = os.path.join(GAMES_DIR, f"{day.isoformat()}.json")
    if os.path.exists(path) and not force:
        return False

    found = games_on(day)
    if found is None:
        log(f"{day}: scoreboard unavailable, skipping (will retry next run)")
        return False
    if not found:
        return False

    log(f"{day}: {len(found)} final game(s)")
    games = []
    for game_id, code in found:
        bs = box_score(game_id)
        if bs is None:
            log(f"  {game_id} ({code}) skipped")
            continue
        bs["gameCode"] = code
        games.append(bs)
        log(f"  {game_id} ({code}) {len(bs['players'])} players")

    if not games:
        return False

    os.makedirs(GAMES_DIR, exist_ok=True)
    payload = {"date": day.isoformat(), "pulledAt": datetime.now().isoformat(timespec="seconds"), "games": games}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return True


# ----------------------------------------------------------------------
# ESPN: injuries and odds
# ----------------------------------------------------------------------
def espn_snapshot():
    """Injuries and betting lines for today's and tomorrow's slates."""
    injuries, odds = {}, []
    for offset in (0, 1):
        day = (date.today() + timedelta(days=offset)).strftime("%Y%m%d")
        try:
            sb = requests.get(ESPN_SCOREBOARD, params={"dates": day}, timeout=30).json()
        except Exception as exc:
            log(f"ESPN scoreboard {day} failed: {exc}")
            continue

        for ev in sb.get("events", []):
            try:
                s = requests.get(ESPN_SUMMARY, params={"event": ev["id"]}, timeout=30).json()
            except Exception as exc:
                log(f"ESPN summary {ev['id']} failed: {exc}")
                continue

            for block in s.get("injuries", []):
                abbr = block.get("team", {}).get("abbreviation")
                for item in block.get("injuries", []):
                    ath = item.get("athlete", {})
                    injuries.setdefault(abbr, []).append({
                        "name": ath.get("displayName"),
                        "id": ath.get("id"),
                        "status": item.get("status"),
                        "detail": (item.get("details") or {}).get("detail"),
                        "returnDate": (item.get("details") or {}).get("returnDate"),
                    })

            comp = (s.get("header", {}).get("competitions") or [{}])[0]
            line = (s.get("pickcenter") or [{}])[0]
            odds.append({
                "espnEventId": ev["id"],
                "name": ev.get("name"),
                "date": ev.get("date"),
                "spread": line.get("spread"),
                "overUnder": line.get("overUnder"),
                "provider": (line.get("provider") or {}).get("name"),
                "neutralSite": comp.get("neutralSite"),
            })
            time.sleep(0.3)

    os.makedirs(DATA, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(DATA, "injuries.json"), "w", encoding="utf-8") as fh:
        json.dump({"pulledAt": stamp, "teams": injuries}, fh, separators=(",", ":"))
    with open(os.path.join(DATA, "odds.json"), "w", encoding="utf-8") as fh:
        json.dump({"pulledAt": stamp, "games": odds}, fh, separators=(",", ":"))

    log(f"ESPN: injuries for {len(injuries)} team(s), odds for {len(odds)} game(s)")


# ----------------------------------------------------------------------
# git
# ----------------------------------------------------------------------
def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def push():
    status = git("status", "--porcelain", "data")
    if not status.stdout.strip():
        log("git: nothing changed")
        return
    git("add", "data")
    msg = f"data: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    commit = git("commit", "-m", msg)
    if commit.returncode != 0 and "nothing to commit" not in commit.stdout.lower():
        log(f"git commit failed: {commit.stdout}{commit.stderr}")
        return
    result = git("push")
    if result.returncode != 0:
        log(f"git push failed: {result.stderr.strip()}")
    else:
        log("git: pushed")


# ----------------------------------------------------------------------
# ---------------------------------------------------------------------------
# logs.json: one compact file the app's Worker reads (last 30 regular-season
# games per player, newest first). Keyed by normalized name so ESPN names match.
# ---------------------------------------------------------------------------
def norm_name(s):
    import re, unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    toks = [t for t in re.sub(r"[^a-z ]", " ", s).split() if t not in ("jr", "sr", "ii", "iii", "iv")]
    return "".join(toks)


def build_logs():
    import glob
    per = {}
    seen = set()
    for f in sorted(glob.glob(os.path.join(GAMES_DIR, "*.json"))):
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        for g in d.get("games", []):
            if not str(g.get("gameId", "")).startswith("002"):
                continue  # regular season only
            code = g.get("gameCode", "")
            try:
                away, home = code.split("/")[1][:3], code.split("/")[1][3:]
            except Exception:
                continue
            for p in g.get("players", []):
                m = p.get("minutes") or ""
                if ":" not in m:
                    continue
                if (g["gameId"], p.get("personId")) in seen:
                    continue
                seen.add((g["gameId"], p.get("personId")))
                a, b = m.split(":")[:2]
                mins = int(a) + int(b) / 60
                if mins <= 0:
                    continue
                t = p.get("teamTricode")
                name = f"{p.get('firstName', '')} {p.get('familyName', '')}".strip()
                rec = per.setdefault(norm_name(name), {"name": name, "pid": p.get("personId"), "team": t, "pos": p.get("position", ""), "log": []})
                rec["team"], rec["pos"] = t, p.get("position", "") or rec["pos"]
                rec["log"].append({
                    "date": d["date"], "opp": away if t == home else home, "home": t == home, "min": round(mins, 1),
                    "pts": p.get("points", 0), "reb": p.get("reboundsTotal", 0), "ast": p.get("assists", 0),
                    "stl": p.get("steals", 0), "blk": p.get("blocks", 0), "tpm": p.get("threePointersMade", 0),
                    "to": p.get("turnovers", 0), "fgm": p.get("fieldGoalsMade", 0), "fga": p.get("fieldGoalsAttempted", 0),
                    "ftm": p.get("freeThrowsMade", 0), "fta": p.get("freeThrowsAttempted", 0),
                    "usg": p.get("usagePercentage"),
                })
    for rec in per.values():
        rec["log"].sort(key=lambda x: x["date"], reverse=True)
        rec["log"] = rec["log"][:30]
    out = {"asOf": datetime.now().isoformat(timespec="seconds"), "players": per}
    with open(os.path.join(DATA, "logs.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    log(f"logs.json: {len(per)} players")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="scan the whole season")
    ap.add_argument("--days", type=int, default=3, help="how many days back (default 3)")
    ap.add_argument("--force", action="store_true", help="rewrite dates already saved")
    ap.add_argument("--no-push", action="store_true", help="skip git commit and push")
    ap.add_argument("--no-espn", action="store_true", help="skip injuries and odds")
    args = ap.parse_args()

    os.makedirs(GAMES_DIR, exist_ok=True)
    os.makedirs(SEASON_DIR, exist_ok=True)

    yesterday = date.today() - timedelta(days=1)
    start = SEASON_START if args.backfill else yesterday - timedelta(days=args.days - 1)

    log(f"=== pull start: {start} -> {yesterday} ({'backfill' if args.backfill else 'incremental'})")

    written = 0
    day = start
    while day <= yesterday:
        try:
            if pull_date(day, force=args.force):
                written += 1
        except Exception:
            log(f"{day}: unhandled error\n{traceback.format_exc()}")
        day += timedelta(days=1)

    if not args.no_espn:
        espn_snapshot()

    try:
        build_logs()
    except Exception:
        log(f"logs.json: error\n{traceback.format_exc()}")

    with open(os.path.join(DATA, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "lastRun": datetime.now().isoformat(timespec="seconds"),
            "lastGameDate": yesterday.isoformat(),
            "datesWritten": written,
        }, fh, indent=2)

    log(f"=== done: {written} date file(s) written")

    if not args.no_push:
        push()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(1)
