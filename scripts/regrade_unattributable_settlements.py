#!/usr/bin/env python3
# Created: 2026-09-03
# Last reused or audited: 2026-09-03
# Authority basis: 2026-09-03 receipt-closure fix to
#   src.analysis.settlement_skill_attribution._resolve_decision_q_from_certificate
#   (grader-q-from-verified-cert). Before the fix, a global-auction certificate
#   whose receipt closure was incomplete — partial declaration on a
#   pre-2026-08-09 certificate, or a schema21 decision_log row pruned by
#   scripts/migrations/202608_decision_log_retention.py's 30-day retention —
#   erased an otherwise identity- and payload_hash-VERIFIED certificate's
#   q_live wholesale, grading the position UNATTRIBUTABLE_Q_MISSING even
#   though the certificate's decision-time belief was fully known. The fix
#   makes receipt closure an audit-only signal (recorded as receipt_closure in
#   derivation_note); q_live now resolves whenever identity + payload_hash
#   verify. Positions already persisted UNATTRIBUTABLE_Q_MISSING under the OLD
#   behaviour are NOT automatically corrected — settlement_attribution's only
#   writer is the incremental grader (run_settlement_skill_attribution), whose
#   only_new=True default skips any position_id that already has a row
#   (src/analysis/settlement_skill_attribution.py: _row_exists /
#   run_settlement_skill_attribution's only_new gate), regardless of category.
#   This is the one-shot targeted re-grade for exactly that backlog.
# WRITER_LOCK: --apply performs DML (persist_grade: INSERT ... ON CONFLICT DO
#   UPDATE, with the prior row archived into
#   settlement_attribution_supersessions first — see persist_grade's own
#   docstring) only, under db_writer_lock(WORLD, BULK) per
#   src/state/db_writer_lock.py, chunked (lock acquired/committed/released per
#   chunk so LIVE writers are never blocked longer than one chunk). Precedent:
#   scripts/migrations/202608_decision_log_retention.py,
#   scripts/enrich_no_trade_regret_outcomes.py (template for this script's
#   dry-run/--apply/chunking shape).
"""Re-grade every settlement_attribution row currently category=
'UNATTRIBUTABLE_Q_MISSING', using the SAME settlement-skill grader
(load_settled_positions) every other settled position uses — NO parallel
grading logic. A row only changes if the certificate now resolves under the
2026-09-03 receipt-closure fix; a row that is genuinely unattributable for an
unrelated reason (absent cert, identity mismatch, payload_hash mismatch, no
q_live) grades UNATTRIBUTABLE_Q_MISSING again and is a no-op on persist
(persist_grade still archives+rewrites, but the content is unchanged).

WHY NOT only_new=False ACROSS THE WHOLE TABLE
----------------------------------------------
A full re-grade (only_new=False) would re-derive and re-persist EVERY settled
position, not just the ~379 UNATTRIBUTABLE_Q_MISSING backlog — needless
supersession-archive churn on the ~735 rows that already graded correctly.
This script computes the SAME grades (via load_settled_positions, which has
no per-position filter) but only *persists* the subset whose CURRENT
settlement_attribution row is UNATTRIBUTABLE_Q_MISSING.

Idempotent: a second run over an already-corrected backlog persists the same
category again (persist_grade's ON CONFLICT DO UPDATE), so re-running is safe
but produces no further category migration.

Usage:
    python3 scripts/regrade_unattributable_settlements.py [--limit N]
        [--chunk-size N] [--db PATH] [--fcst-db PATH] [--apply]

Default is dry-run (report only, no writes). --apply performs the UPDATEs.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 200

# The market-anchored admission calibrator's training-row predicate
# (src/calibration/market_anchored_live_fit.py load_fit_rows) — used only to
# report how the training set would move, never to gate this script's writes.
_FIT_ROW_PREDICATE_SQL = """
    SELECT COUNT(*) FROM settlement_attribution
     WHERE q_in_bin IS NOT NULL
       AND market_in_bin_prob IS NOT NULL
       AND settled_in_bin IS NOT NULL
       AND direction IS NOT NULL
"""


def _attach_forecasts(conn: sqlite3.Connection, fcst_db_path: Path) -> None:
    attached = {row[1] for row in conn.execute("PRAGMA database_list").fetchall()}
    if "forecasts" not in attached:
        conn.execute("ATTACH DATABASE ? AS forecasts", (str(fcst_db_path),))


def _fit_row_would_qualify(grade) -> bool:
    return (
        grade.q_in_bin is not None
        and grade.market_in_bin_prob is not None
        and grade.settled_in_bin is not None
        and grade.direction is not None
    )


def run(
    *,
    world_db_path: Path,
    fcst_db_path: Path,
    limit: int | None,
    chunk_size: int,
    apply: bool,
) -> dict[str, object]:
    from src.analysis.settlement_skill_attribution import (
        load_settled_positions,
        persist_grade,
    )
    from src.state.db_writer_lock import WriteClass, db_writer_lock

    if apply:
        conn = sqlite3.connect(str(world_db_path))
    else:
        conn = sqlite3.connect(f"file:{world_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    stats: dict[str, object] = {
        "unattributable_before": 0,
        "candidates_seen": 0,
        "still_unattributable": 0,
        "reclassified": 0,
        "new_category_distribution": {},
        "fit_rows_before": 0,
        "fit_rows_after_estimate": 0,
        "persisted": 0,
    }
    try:
        _attach_forecasts(conn, fcst_db_path)

        target_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT position_id FROM settlement_attribution"
                " WHERE category = 'UNATTRIBUTABLE_Q_MISSING'"
            ).fetchall()
        }
        stats["unattributable_before"] = len(target_ids)
        stats["fit_rows_before"] = int(
            conn.execute(_FIT_ROW_PREDICATE_SQL).fetchone()[0]
        )

        if not target_ids:
            return stats

        # load_settled_positions has no per-position filter; regrade every
        # settled position and keep only the ones already flagged
        # UNATTRIBUTABLE_Q_MISSING above. This reuses the ONE grading function
        # — no parallel grading logic — at the cost of regrading rows we then
        # discard; correctness over cheapness for a one-shot backfill.
        all_grades = load_settled_positions(conn, only_new=False)
        candidates = [g for g in all_grades if g.position_id in target_ids]
        if limit is not None:
            candidates = candidates[:limit]
        stats["candidates_seen"] = len(candidates)

        newly_qualifying_fit_rows = 0
        to_persist: list = []
        for g in candidates:
            dist = stats["new_category_distribution"]
            dist[g.category] = dist.get(g.category, 0) + 1
            if g.category == "UNATTRIBUTABLE_Q_MISSING":
                stats["still_unattributable"] += 1
            else:
                stats["reclassified"] += 1
            if _fit_row_would_qualify(g):
                newly_qualifying_fit_rows += 1
            to_persist.append(g)

        stats["fit_rows_after_estimate"] = (
            stats["fit_rows_before"] + newly_qualifying_fit_rows
        )

        if apply:
            now_utc = datetime.now(tz=timezone.utc)
            for start in range(0, len(to_persist), chunk_size):
                chunk = to_persist[start : start + chunk_size]
                with db_writer_lock(world_db_path, WriteClass.BULK):
                    for g in chunk:
                        persist_grade(conn, g, now_utc=now_utc)
                        stats["persisted"] += 1
                    conn.commit()
                logger.info(
                    "chunk done: persisted=%d/%d",
                    stats["persisted"], len(to_persist),
                )
    finally:
        conn.close()

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write UPDATEs (default: dry-run report only).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max UNATTRIBUTABLE_Q_MISSING rows to regrade.",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="World DB path (default: canonical zeus-world.db).",
    )
    parser.add_argument(
        "--fcst-db", type=Path, default=None,
        help="Forecasts DB path (default: canonical zeus-forecasts.db).",
    )
    args = parser.parse_args()

    from src.state.db import ZEUS_FORECASTS_DB_PATH, ZEUS_WORLD_DB_PATH

    world_db_path = args.db or ZEUS_WORLD_DB_PATH
    fcst_db_path = args.fcst_db or ZEUS_FORECASTS_DB_PATH

    logger.info(
        "world_db=%s fcst_db=%s apply=%s limit=%s chunk_size=%d",
        world_db_path, fcst_db_path, args.apply, args.limit, args.chunk_size,
    )

    stats = run(
        world_db_path=world_db_path,
        fcst_db_path=fcst_db_path,
        limit=args.limit,
        chunk_size=args.chunk_size,
        apply=args.apply,
    )

    print(f"{'[APPLY] ' if args.apply else '[DRY-RUN] '}UNATTRIBUTABLE_Q_MISSING re-grade")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if not args.apply:
        print(
            f"\n[dry-run] would reclassify {stats['reclassified']} of "
            f"{stats['candidates_seen']} candidate rows; "
            f"{stats['still_unattributable']} remain genuinely unattributable "
            f"(no writes made)"
        )


if __name__ == "__main__":
    main()
