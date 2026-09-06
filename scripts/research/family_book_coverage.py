"""Coverage census for a family_book_capture.py database.

Answers, before any book-only replay runs, whether the feed can support it:
  - per family: fraction of instants (1-minute grid) at which EVERY bin of the family is VALID
    (its token was initialised — a ``book`` snapshot arrived — in the connection epoch active at
    that instant, and the feed was connected at that instant). An unchanged quote on a healthy
    connection counts as valid: validity is a state, not "a row landed in the last fresh_s";
  - median gap between consecutive top-of-book CHANGES per token;
  - ask-only share (rows with an ask but no bid);
  - feed health summary (connected minutes, msgs/min, reconnects).

Families and their token sets come from ``token_meta`` (the subscribed universe), so a
subscribed family with zero rows stays in the denominator at 0 coverage rather than vanishing.

Usage: .venv/bin/python scripts/research/family_book_coverage.py --db <path> [--fresh-s 60]
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _validity_fn(conn: sqlite3.Connection):
    """Build validity(token_id, instant_epoch_s) -> bool from connection_epoch + feed_health.

    A token is valid at an instant when: (a) the instant falls within the time window of some
    connection epoch (the largest epoch whose earliest book_top row is at or before the instant);
    (b) that token has a book_top row in that same epoch at or before the instant (i.e. it was
    initialised by a ``book`` snapshot this epoch); and (c) the feed's most recent feed_health
    tick at or before the instant reports connected=1.
    """
    epoch_rows = conn.execute(
        "SELECT connection_epoch AS e, MIN(received_at_utc) AS m FROM book_top GROUP BY connection_epoch"
    ).fetchall()
    epoch_starts = sorted((r["e"], _dt(r["m"]).timestamp()) for r in epoch_rows if r["e"] is not None)
    epoch_start_times = [t for _, t in epoch_starts]
    epoch_ids = [e for e, _ in epoch_starts]

    init_rows = conn.execute(
        "SELECT token_id AS t, connection_epoch AS e, MIN(received_at_utc) AS m FROM book_top GROUP BY token_id, connection_epoch"
    ).fetchall()
    token_epoch_init: dict[tuple[str, int], float] = {
        (r["t"], r["e"]): _dt(r["m"]).timestamp() for r in init_rows if r["e"] is not None
    }

    health_rows = conn.execute("SELECT received_at_utc, connected FROM feed_health ORDER BY received_at_utc").fetchall()
    health_times = [_dt(r["received_at_utc"]).timestamp() for r in health_rows]
    health_connected = [bool(r["connected"]) for r in health_rows]

    def epoch_at(instant: float) -> int | None:
        i = bisect.bisect_right(epoch_start_times, instant) - 1
        return epoch_ids[i] if i >= 0 else None

    def connected_at(instant: float) -> bool:
        i = bisect.bisect_right(health_times, instant) - 1
        return health_connected[i] if i >= 0 else False

    def validity(token_id: str, instant: float) -> bool:
        e = epoch_at(instant)
        if e is None:
            return False
        init = token_epoch_init.get((token_id, e))
        if init is None or init > instant:
            return False
        return connected_at(instant)

    return validity


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
    validity = _validity_fn(conn)
    conn.close()
    if not rows:
        return {"rows": 0}

    by_token: dict[str, list[datetime]] = defaultdict(list)
    fam_tokens: dict[str, set[str]] = defaultdict(set)
    ask_only = 0
    for token_id, m in meta.items():
        fam_tokens[m.get("event_slug") or "?"].add(token_id)
    for r in rows:
        ts = _dt(r["received_at_utc"])
        by_token[r["token_id"]].append(ts)
        slug = r["event_slug"] or meta.get(r["token_id"], {}).get("event_slug") or "?"
        fam_tokens[slug].add(r["token_id"])
        if r["best_ask"] is not None and r["best_bid"] is None:
            ask_only += 1

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
        for k in range(n_grid):
            instant = t0.timestamp() + k * grid_s
            if all(validity(t, instant) for t in tokens):
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
