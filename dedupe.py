"""One-time cleanup: remove duplicate games from every data/games/*.json file.
Run from the repo folder:  python dedupe.py   then  python pull.py --days 1  (rebuilds logs.json)"""
import glob, json, os

fixed = removed = 0
for f in sorted(glob.glob(os.path.join("data", "games", "*.json"))):
    with open(f, encoding="utf-8") as fh:
        d = json.load(fh)
    seen, keep = set(), []
    for g in d.get("games", []):
        if g["gameId"] in seen:
            removed += 1
            continue
        seen.add(g["gameId"])
        keep.append(g)
    if len(keep) != len(d["games"]):
        d["games"] = keep
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        fixed += 1
print(f"removed {removed} duplicate game(s) from {fixed} file(s)")
