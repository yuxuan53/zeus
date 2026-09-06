"""Coverage census for a family_book_capture.py database.

Answers, before any book-only replay runs, whether the feed can support it:
  - per family: fraction of instants (1-minute grid) at which EVERY bin of the family has a
    top-of-book observation within the last 60 s;
  - median gap between consecutive top-of-book CHANGES per token;
  - ask-only share (rows with an ask but no bid);
  - feed health summary (connected minutes, msgs/min, reconnects).

Usage: .venv/bin/python scripts/research/family_book_coverage.py --db <path> [--fresh-s 60]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def census(db_path: str, *, fresh_s: float = 60.0, grid_s: float = 60.0) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT token_id, event_slug, received_at_utc, best_bid, best_ask FROM book_top ORDER BY received_at_utc"
    ).fetchall()
    meta = {r["token_id"]: dict(r) for r in conn.execute("SELECT token_id, event_slug, outcome_label FROM token_meta")}
    health = conn.execute(
        "SELECT COUNT(*) n, SUM(connected) up, AVG(msgs_per_min) mpm, MAX(reconnects) rc FROM feed_health"
    ).fetchone()
    conn.close()
    if not rows:
        return {"rows": 0}

    by_token: dict[str, list[datetime]] = defaultdict(list)
    fam_tokens: dict[str, set[str]] = defaultdict(set)
    ask_only = 0
    for r in rows:
        ts = _dt(r["received_at_utc"])
        by_token[r["token_id"]].append(ts)
        slug = r["event_slug"] or meta.get(r["token_id"], {}).get("event_slug") or "?"
        fam_tokens[slug].add(r["token_id"])
        if r["best_ask"] is not None and r["best_bid"] is None:
            ask_only += 1
    for slug, tokens in list(fam_tokens.items()):
        for token_id, m in meta.items():
            if m.get("event_slug") == slug:
                tokens.add(token_id)

    t0 = _dt(rows[0]["received_at_utc"])
    t1 = _dt(rows[-1]["received_at_utc"])
    span_s = max(grid_s, (t1 - t0).total_seconds())
    n_grid = int(span_s // grid_s) + 1

    gaps: list[float] = []
    for times in by_token.values():
        gaps.extend((b - a).total_seconds() for a, b in zip(times, times[1:], strict=False))

    families = {}
    for slug, tokens in fam_tokens.items():
        fresh_hits = 0
        cursors = {t: 0 for t in tokens}
        last_seen: dict[str, datetime | None] = dict.fromkeys(tokens)
        for k in range(n_grid):
            instant = t0.timestamp() + k * grid_s
            for t in tokens:
                seq = by_token.get(t, [])
                i = cursors[t]
                while i < len(seq) and seq[i].timestamp() <= instant:
                    last_seen[t] = seq[i]
                    i += 1
                cursors[t] = i
            if all(ls is not None and instant - ls.timestamp() <= fresh_s for ls in last_seen.values()):
                fresh_hits += 1
        families[slug] = {
            "tokens": len(tokens),
            "tokens_with_rows": sum(1 for t in tokens if by_token.get(t)),
            "all_fresh_fraction": round(fresh_hits / n_grid, 3),
        }

    return {
        "rows": len(rows),
        "tokens": len(by_token),
        "families": len(fam_tokens),
        "span_minutes": round(span_s / 60.0, 1),
        "ask_only_share": round(ask_only / len(rows), 3),
        "median_change_gap_s": round(statistics.median(gaps), 1) if gaps else None,
        "p90_change_gap_s": round(sorted(gaps)[int(0.9 * (len(gaps) - 1))], 1) if gaps else None,
        "all_fresh_fraction_median": round(statistics.median(f["all_fresh_fraction"] for f in families.values()), 3),
        "health": {
            "minutes": health["n"], "connected_minutes": health["up"],
            "msgs_per_min_avg": round(health["mpm"] or 0.0, 1), "reconnects": health["rc"] or 0,
        },
        "per_family": families,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--fresh-s", type=float, default=60.0)
    parser.add_argument("--top", type=int, default=12, help="families to print")
    args = parser.parse_args(argv)
    out = census(args.db, fresh_s=args.fresh_s)
    if out.get("rows", 0) == 0:
        print("no book_top rows")
        return 1
    hdr = {k: v for k, v in out.items() if k != "per_family"}
    for k, v in hdr.items():
        print(f"{k}: {v}")
    print("per_family (worst all_fresh first):")
    for slug, f in sorted(out["per_family"].items(), key=lambda kv: kv[1]["all_fresh_fraction"])[: args.top]:
        print(f"  {f['all_fresh_fraction']:.3f}  tokens={f['tokens']:2d} with_rows={f['tokens_with_rows']:2d}  {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
