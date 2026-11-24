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
  python pull.py --history 5     # one-off: last 5 past seasons of game logs into data/history/

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
    LeagueGameLog,
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
HISTORY_DIR = os.path.join(DATA, "history")
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
                    # A team on today's and tomorrow's slate appears twice; keep one entry per player.
                    if any(x.get("id") == ath.get("id") for x in injuries.get(abbr, [])):
                        continue
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
# history: whole past seasons in two calls each (LeagueGameLog)
# ----------------------------------------------------------------------
def season_label(start_year):
    return f"{start_year}-{str(start_year + 1)[2:]}"


def pull_history(n, force=False):
    """Past n regular seasons: every player game and every team game.
    Writes data/history/<season>_players.json.gz and _teams.json.gz as
    {"season", "cols", "rows"} (compact). Seasons already saved are skipped."""
    import gzip
    os.makedirs(HISTORY_DIR, exist_ok=True)
    first = SEASON_START.year - 1
    wrote = 0
    for y in range(first, first - n, -1):
        season = season_label(y)
        for kind, flag in (("players", "P"), ("teams", "T")):
            path = os.path.join(HISTORY_DIR, f"{season}_{kind}.json.gz")
            if os.path.exists(path) and not force:
                log(f"history {season} {kind}: already saved")
                continue
            ep = retry(lambda: LeagueGameLog(season=season, season_type_all_star="Regular Season",
                                             player_or_team_abbreviation=flag, timeout=TIMEOUT),
                       f"history {season} {kind}")
            time.sleep(REQUEST_PAUSE * 3)
            data = rows(ep, "LeagueGameLog")
            if not data:
                log(f"history {season} {kind}: no rows (call failed or empty)")
                continue
            cols = list(data[0].keys())
            out = {"season": season, "pulledAt": datetime.now().isoformat(timespec="seconds"),
                   "cols": cols, "rows": [[r.get(c) for c in cols] for r in data]}
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
            log(f"history {season} {kind}: {len(data)} rows -> {os.path.basename(path)}")
            wrote += 1
    return wrote


# ----------------------------------------------------------------------
# player_priors.json: last season's per-game average and game-to-game spread
# for every player, so the app's prop model has a starting point in the first
# weeks of a season. Built from data/history; keyed like the Worker's normName.
# ----------------------------------------------------------------------
PRIOR_STATS = {"pts": ["PTS"], "reb": ["REB"], "ast": ["AST"], "tpm": ["FG3M"], "stl": ["STL"], "blk": ["BLK"],
               "pra": ["PTS", "REB", "AST"], "pa": ["PTS", "AST"], "pr": ["PTS", "REB"], "ra": ["REB", "AST"], "sb": ["STL", "BLK"]}


def prior_key(name):
    import unicodedata, re
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z ]", " ", s)
    return "".join(t for t in s.split() if t and t not in ("jr", "sr", "ii", "iii", "iv"))


def build_priors():
    import gzip, statistics
    season = season_label(SEASON_START.year - 1)
    path = os.path.join(HISTORY_DIR, f"{season}_players.json.gz")
    if not os.path.exists(path):
        log(f"player_priors: no history for {season}; run --history first")
        return
    d = json.load(gzip.open(path, "rt", encoding="utf-8"))
    ix = {c: i for i, c in enumerate(d["cols"])}
    per = {}
    for r in d["rows"]:
        if (r[ix["MIN"]] or 0) < 10:
            continue
        per.setdefault(r[ix["PLAYER_ID"]], {"name": r[ix["PLAYER_NAME"]], "games": []})["games"].append(r)
    out = {}
    for pid, v in per.items():
        gs = v["games"]
        if len(gs) < 15:
            continue
        m, sd = {}, {}
        for k, cols in PRIOR_STATS.items():
            vals = [sum((g[ix[c]] or 0) for c in cols) for g in gs]
            m[k] = round(statistics.mean(vals), 2)
            sd[k] = round(statistics.stdev(vals), 2)
        out[prior_key(v["name"])] = {"id": pid, "g": len(gs), "min": round(statistics.mean(g[ix["MIN"]] for g in gs), 1), "m": m, "s": sd}
    with open(os.path.join(DATA, "player_priors.json"), "w", encoding="utf-8") as fh:
        json.dump({"season": season, "players": out}, fh, separators=(",", ":"), ensure_ascii=False)
    log(f"player_priors.json: {len(out)} players from {season}")


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
    pulled = git("pull", "--rebase", "--autostash")
    if pulled.returncode != 0:
        log(f"git pull --rebase failed: {pulled.stderr.strip()}")
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


# ----------------------------------------------------------------------
# Positions and defence by position
# ----------------------------------------------------------------------
DVP_STATS = ("pts", "reb", "ast", "tpm", "stl", "blk")
DVP_PRIOR_GAMES = 10   # team-wide ratio: shrink toward 1.0 by this many average player-games
DVP_POS_GAMES = 40     # position ratio: shrink toward the team-wide ratio by this many


def player_index_positions():
    """NBA.com player index: one call, POSITION for every player ('G', 'G-F', 'F-C', 'C').
    Returns {personId: 'G'|'F'|'C'} using the first listed position, or {} on failure."""
    try:
        from nba_api.stats.endpoints import PlayerIndex
        d = retry(lambda: PlayerIndex(league_id="00", season=season_label(SEASON_START.year), timeout=TIMEOUT).get_normalized_dict(), "player index")
        out = {}
        for r in (d or {}).get("PlayerIndex", []):
            pos = (r.get("POSITION") or "").strip()
            if pos and pos[0] in "GFC":
                out[r.get("PERSON_ID")] = pos[0]
        log(f"player index: {len(out)} positions")
        return out
    except Exception:
        log(f"player index: failed, using box-score starts and stat guesses\n{traceback.format_exc()}")
        return {}


def guess_position(logrows):
    """Last resort for a bench player NBA.com did not list: per-36 rebounds/blocks/assists."""
    m = sum(g["min"] for g in logrows) or 1
    p36 = lambda k: sum(g.get(k, 0) for g in logrows) * 36 / m
    if p36("reb") >= 10.5 or p36("blk") >= 1.8:
        return "C"
    if p36("ast") >= 5.0:
        return "G"
    return "F"


def assign_positions(per, index=None):
    """G / F / C for every player: most frequent starting position, else the NBA.com
    player index, else a guess from his stats. Writes rec['pos'] and rec['posFrom']."""
    if index is None:
        index = player_index_positions()
    for rec in per.values():
        starts = rec.get("_starts") or {}
        if starts:
            rec["pos"], rec["posFrom"] = max(starts, key=starts.get), "starts"
        elif index.get(rec.get("pid")):
            rec["pos"], rec["posFrom"] = index[rec["pid"]], "index"
        elif rec["log"]:
            rec["pos"], rec["posFrom"] = guess_position(rec["log"]), "guess"


def dvp_table(per, before=None, since=None):
    """Defence by position, measured against expectation.

    For every player-game: expected stat = the player's own per-minute rate in his OTHER
    games x his minutes that night. For each opponent and position (G/F/C) the ratio
    sum(actual) / sum(expected) says how much that defence gives up versus normal:
    1.08 = 8% more than those players usually produce. Each ratio is shrunk toward 1.0 by
    DVP_PRIOR_GAMES average player-games. Optional before/since limit the dates (backtest)."""
    totals = {}
    for rec in per.values():
        rows = [g for g in rec["log"] if (before is None or g["date"] < before) and (since is None or g["date"] >= since)]
        rows_all = [g for g in rec["log"] if before is None or g["date"] < before]
        pos = rec.get("pos")
        if pos not in ("G", "F", "C") or len(rows_all) < 5:
            continue
        mins = sum(g["min"] for g in rows_all)
        if mins < 60:
            continue
        sums = {k: sum(g.get(k, 0) for g in rows_all) for k in DVP_STATS}
        for g in rows:
            if g["min"] < 6 or mins - g["min"] <= 0:
                continue
            cell = totals.setdefault(g["opp"], {}).setdefault(pos, {"n": 0, "a": {k: 0.0 for k in DVP_STATS}, "e": {k: 0.0 for k in DVP_STATS}})
            cell["n"] += 1
            for k in DVP_STATS:
                cell["a"][k] += g.get(k, 0)
                cell["e"][k] += (sums[k] - g.get(k, 0)) / (mins - g["min"]) * g["min"]  # leave this game out
    # Two levels: the team's ratio over all positions (shrunk toward 1.0), then each
    # position's ratio shrunk toward its team's. Early in the season a position has few
    # games and leans on the team number; it earns its own as the games pile up.
    keys = DVP_STATS + ("pra",)
    val = lambda c, k: sum(c[k2] for k2 in ("pts", "reb", "ast")) if k == "pra" else c[k]
    avg = {}
    for pos in ("G", "F", "C", "all"):
        cells = [c for t in totals.values() for p2, c in t.items() if pos in (p2, "all")]
        n = sum(c["n"] for c in cells) or 1
        avg[pos] = {k: sum(val(c["e"], k) for c in cells) / n for k in keys}
    out = {}
    for team, bypos in totals.items():
        A = {k: sum(val(c["a"], k) for c in bypos.values()) for k in keys}
        E = {k: sum(val(c["e"], k) for c in bypos.values()) for k in keys}
        teamr = {}
        for k in keys:
            K = DVP_PRIOR_GAMES * avg["all"][k]
            teamr[k] = (A[k] + K) / (E[k] + K) if E[k] + K > 0 else 1.0
        out[team] = {"all": {k: round(teamr[k], 3) for k in keys}}
        out[team]["all"]["n"] = sum(c["n"] for c in bypos.values())
        for pos, c in bypos.items():
            r = {}
            for k in keys:
                K = DVP_POS_GAMES * avg[pos][k]
                r[k] = round((val(c["a"], k) + K * teamr[k]) / (val(c["e"], k) + K), 3) if val(c["e"], k) + K > 0 else round(teamr[k], 3)
            r["n"] = c["n"]
            out[team][pos] = r
    for pos in ("G", "F", "C", "all"):  # rank 1 = gives up least = toughest matchup
        order = sorted((t for t in out if pos in out[t]), key=lambda t: out[t][pos]["pra"])
        for i, t in enumerate(order):
            out[t][pos]["rank"] = i + 1
    return out


DVP_BETA_PRIOR = {"pts": 0.3, "reb": 0.2, "ast": 0.4, "tpm": 0.2, "stl": 0.2, "blk": 0.2, "pra": 0.35}
DVP_BETA_WEIGHT = 2000  # player-games the prior is worth when blending with the measured beta


def fit_dvp_beta(per, max_dates=20):
    """How strongly each stat follows the matchup, measured walk-forward on this season:
    for each recent date D, ratios from games before D vs how far players landed from
    their own expectation on D. Least-squares beta in actual/expected ~ ratio ** beta,
    blended with DVP_BETA_PRIOR by sample size and kept within 0..0.8. The app multiplies
    a projection by ratio ** beta, so 0 = ignore the matchup, 1 = take it at face value."""
    import math
    dates = sorted({g["date"] for r in per.values() for g in r["log"]})
    test = dates[len(dates) // 2:][-max_dates:]
    keys = tuple(DVP_BETA_PRIOR)
    xs = {k: [] for k in keys}
    for D in test:
        table = dvp_table(per, before=D)
        for rec in per.values():
            pos = rec.get("pos")
            prior = [g for g in rec["log"] if g["date"] < D]
            if pos not in ("G", "F", "C") or len(prior) < 5:
                continue
            mins = sum(g["min"] for g in prior)
            for g in rec["log"]:
                if g["date"] != D or g["min"] < 15:
                    continue
                cell = table.get(g["opp"], {}).get(pos)
                if not cell:
                    continue
                for k in keys:
                    ks = ("pts", "reb", "ast") if k == "pra" else (k,)
                    exp = sum(sum(h.get(s2, 0) for h in prior) for s2 in ks) / mins * g["min"]
                    if exp >= 1.0:
                        xs[k].append((math.log(cell[k]), math.log((sum(g.get(s2, 0) for s2 in ks) + 0.5) / (exp + 0.5))))
    out = {}
    for k in keys:
        v, prior = xs[k], DVP_BETA_PRIOR[k]
        if len(v) < 50:
            out[k] = {"beta": prior, "measured": None, "n": len(v)}
            continue
        mx = sum(a for a, _ in v) / len(v); my = sum(b for _, b in v) / len(v)
        sxx = sum((a - mx) ** 2 for a, _ in v); sxy = sum((a - mx) * (b - my) for a, b in v)
        b = sxy / sxx if sxx else 0.0
        w = len(v) / (len(v) + DVP_BETA_WEIGHT)
        out[k] = {"beta": round(min(0.8, max(0.0, w * b + (1 - w) * prior)), 3), "measured": round(b, 3), "n": len(v)}
    return out


def build_dvp(per):
    table = dvp_table(per)
    try:
        beta = fit_dvp_beta(per)
    except Exception:
        log(f"dvp beta fit failed, using priors\n{traceback.format_exc()}")
        beta = {k: {"beta": v, "measured": None, "n": 0} for k, v in DVP_BETA_PRIOR.items()}
    src = {}
    for rec in per.values():
        src[rec.get("posFrom", "none")] = src.get(rec.get("posFrom", "none"), 0) + 1
    out = {"asOf": datetime.now().isoformat(timespec="seconds"), "priorGames": DVP_PRIOR_GAMES, "posGames": DVP_POS_GAMES,
           "stats": list(DVP_STATS) + ["pra"], "beta": beta, "positionsFrom": src, "teams": table}
    with open(os.path.join(DATA, "dvp.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    log(f"dvp.json: {len(table)} teams; positions from {src}")


def build_logs():
    per = load_player_games()
    assign_positions(per)
    try:
        build_dvp(per)
    except Exception:
        log(f"dvp.json: failed, app keeps team-wide defence\n{traceback.format_exc()}")
    for rec in per.values():
        rec["log"] = rec["log"][:30]
        rec.pop("_starts", None)
    out = {"asOf": datetime.now().isoformat(timespec="seconds"), "players": per}
    with open(os.path.join(DATA, "logs.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    log(f"logs.json: {len(per)} players")


def load_player_games():
    """Every regular-season player-game this season from data/games, newest first per player."""
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
                rec = per.setdefault(norm_name(name), {"name": name, "pid": p.get("personId"), "team": t, "pos": "", "log": []})
                rec["team"] = t
                if p.get("position"):  # only starters get one in the box score; count them
                    starts = rec.setdefault("_starts", {})
                    starts[p["position"]] = starts.get(p["position"], 0) + 1
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
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="scan the whole season")
    ap.add_argument("--days", type=int, default=3, help="how many days back (default 3)")
    ap.add_argument("--force", action="store_true", help="rewrite dates already saved")
    ap.add_argument("--no-push", action="store_true", help="skip git commit and push")
    ap.add_argument("--no-espn", action="store_true", help="skip injuries and odds")
    ap.add_argument("--history", type=int, default=0, help="one-off: pull this many past seasons, then stop")
    args = ap.parse_args()

    if args.history:
        log(f"=== history: last {args.history} season(s)")
        wrote = pull_history(args.history, force=args.force)
        build_priors()
        log(f"=== history done: {wrote} file(s) written")
        if not args.no_push:
            push()
        return

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
