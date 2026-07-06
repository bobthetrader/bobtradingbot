"""On-demand API provider scoreboard — which provider is fastest/cheapest.

Reads data/api_provider_stats.json (written by core/api_providers.py inside
the bot). Run on the server:
  docker exec tradingbot_local python scripts/api_scoreboard.py
or locally against a DR-restored data dir:
  py scripts/api_scoreboard.py --stats path/to/api_provider_stats.json
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pctile(vals, q):
    if not vals:
        return 0.0
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))]


def main():
    ap = argparse.ArgumentParser()
    default_stats = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", "api_provider_stats.json")
    ap.add_argument("--stats", default=default_stats)
    args = ap.parse_args()

    if not os.path.exists(args.stats):
        print(f"No stats file at {args.stats} — the bot writes it after its first routed call.")
        return

    with open(args.stats, "r", encoding="utf-8") as f:
        snap = json.load(f)

    print(f"{'provider:endpoint':<28} {'calls':>6} {'shadow':>7} {'fails':>6} "
          f"{'ok%':>6} {'p50ms':>7} {'p90ms':>7}  last_error")
    by_provider = {}
    for key in sorted(snap):
        s = snap[key]
        lat = s.get("lat", [])
        calls, fails = s.get("calls", 0), s.get("fails", 0)
        ok = 100.0 * (calls - fails) / calls if calls else 0.0
        err = (s.get("last_error") or "")[:40]
        print(f"{key:<28} {calls:>6} {s.get('shadow_calls',0):>7} {fails:>6} "
              f"{ok:>5.1f}% {pctile(lat,0.5):>7.0f} {pctile(lat,0.9):>7.0f}  {err}")
        prov = key.split(":")[0]
        agg = by_provider.setdefault(prov, {"calls": 0, "fails": 0, "lat": []})
        agg["calls"] += calls
        agg["fails"] += fails
        agg["lat"].extend(lat)

    print()
    verdict = None
    for prov, a in sorted(by_provider.items()):
        ok = 100.0 * (a["calls"] - a["fails"]) / a["calls"] if a["calls"] else 0.0
        p50 = pctile(a["lat"], 0.5)
        cost = "free (rate-limited)" if prov == "coingecko" else \
               f"~{a['calls']} credits used (free tier ~333/day)"
        print(f"{prov}: {a['calls']} calls, {ok:.1f}% ok, p50 {p50:.0f}ms — {cost}")
        if a["calls"] >= 10 and ok >= 99.0 and (verdict is None or p50 < verdict[1]):
            verdict = (prov, p50)

    print()
    if verdict:
        print(f"VERDICT (speed, >=99% ok, n>=10): {verdict[0]} (p50 {verdict[1]:.0f}ms).")
        print("Cost: coingecko is always cheaper (free); prefer CMC only if it is "
              "significantly faster or coingecko's ok% degrades.")
    else:
        print("VERDICT: not enough clean data yet (need >=10 calls at >=99% ok per provider).")
    print(f"\nGenerated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
