"""Backtest for defence by position (pull.py dvp_table).

Walk-forward: for each test date D, build the table from games before D only, then check
the games on D: does the opponent's ratio predict how far players land above or below
their own expected numbers? Fits beta in  actual/expected ~ ratio ** beta  per stat
(least squares on logs), and compares with the team-wide rank the app used before.

  python tools/dvp_fit.py            # uses data/games, no network
"""
import math, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pull

per = pull.load_player_games()
pull.assign_positions(per, index={})  # offline: starts + stat guess
dates = sorted({g["date"] for r in per.values() for g in r["log"]})
test = dates[len(dates) // 2:]
print(f"{len(per)} players, {len(dates)} dates; testing on {len(test)} dates from {test[0]}")
xs = {k: [] for k in pull.DVP_STATS + ("pra",)}
for D in test:
    table = pull.dvp_table(per, before=D)
    for rec in per.values():
        pos = rec.get("pos"); prior = [g for g in rec["log"] if g["date"] < D]
        if pos not in ("G", "F", "C") or len(prior) < 5:
            continue
        mins = sum(g["min"] for g in prior)
        for g in rec["log"]:
            if g["date"] != D or g["min"] < 15:
                continue
            cell = table.get(g["opp"], {}).get(pos)
            if not cell:
                continue
            for k in xs:
                ks = ("pts", "reb", "ast") if k == "pra" else (k,)
                exp = sum(sum(h.get(s, 0) for h in prior) for s in ks) / mins * g["min"]
                act = sum(g.get(s, 0) for s in ks)
                if exp < 1.0:
                    continue
                xs[k].append((math.log(cell[k]), math.log((act + 0.5) / (exp + 0.5))))
print(f"{'stat':5} {'n':>6} {'beta':>6} {'r':>6}   (beta 1 = take the ratio at face value)")
for k, v in xs.items():
    if len(v) < 50:
        continue
    mx = sum(a for a, _ in v) / len(v); my = sum(b for _, b in v) / len(v)
    sxx = sum((a - mx) ** 2 for a, _ in v); sxy = sum((a - mx) * (b - my) for a, b in v); syy = sum((b - my) ** 2 for _, b in v)
    beta = sxy / sxx if sxx else 0; r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0
    print(f"{k:5} {len(v):6d} {beta:6.2f} {r:6.3f}")
