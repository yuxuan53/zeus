# Created: 2026-08-25
# Last reused or audited: 2026-09-09
# Authority basis: docs/operations/current/plans/reversal_plan_tier0_2026-08-24.md
#   item 13 Slice C -- source-content-bound vacuum cutover acceptance.
#   item 13 Slice C -- coverage for scripts/ops/vacuum_reset_trades_db.py's
#   PRECONDITION AND ASSERTION LOGIC ONLY, exercised exclusively against tiny
#   disposable fixture files under tmp_path. This script has never been run
#   against zeus_trades.db or any DB resembling it, live or otherwise -- these
#   tests do not change that; they prove the logic is correct in isolation.
"""Antibodies for vacuum_reset_trades_db.py: the position/entries-paused
precondition, backup-manifest verification, source integrity check,
free-space check, the full --vacuum-into flow (row-count match, size
assertions, auto_vacuum conversion, receipt), the writer-plane fence, and the
full --swap flow (receipt verification, atomic swap, restore-on-failure)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.ops.vacuum_reset_trades_db as vrt


TRADE_DDL = """
CREATE TABLE position_current (
    position_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL
);
CREATE TABLE decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _make_trade_db(
    path: Path,
    *,
    open_positions: int = 0,
    decision_log_rows: int = 0,
    bloat_and_delete_rows: int = 0,
) -> None:
    """``bloat_and_delete_rows`` inserts N large rows then deletes most of
    them (keeping ``decision_log_rows``), simulating a DB where retention
    has already run: with auto_vacuum=0 the freed pages stay in the file
    (internal freelist) rather than shrinking it, so VACUUM INTO has real
    bloat to reclaim -- matching the actual live scenario this script exists
    for, rather than a trivially-empty fixture VACUUM INTO cannot shrink."""
    conn = sqlite3.connect(str(path))
    conn.executescript(TRADE_DDL)
    for i in range(open_positions):
        conn.execute(
            "INSERT INTO position_current VALUES (?, 'active')", (f"pos-{i}",)
        )
    total_rows = max(decision_log_rows, bloat_and_delete_rows)
    for i in range(total_rows):
        conn.execute(
            "INSERT INTO decision_log (mode, payload) VALUES ('settlement', ?)",
            (f"payload-{i}" * 200,),  # pad so the file is non-trivially sized
        )
    if bloat_and_delete_rows:
        conn.execute(
            "DELETE FROM decision_log WHERE id <= ?",
            (bloat_and_delete_rows - decision_log_rows,),
        )
    conn.commit()
    conn.close()


def _make_backup_manifest(
    path: Path, *, db_name: str, created_at: datetime, ok: bool = True
) -> None:
    manifest = {
        "created_at": created_at.isoformat(),
        "entries": [
            {
                "db": db_name,
                "dest": "/tmp/fake_backup_dest.db",
                "dest_sha256": "deadbeef",
                "verify": {"ok": ok, "integrity_check": "ok"},
                "created_at": created_at.isoformat(),
            }
        ],
    }
    path.write_text(json.dumps(manifest))


# ---------------------------------------------------------------------------
# Position / entries-paused precondition
# ---------------------------------------------------------------------------


def test_zero_open_positions_passes(tmp_path: Path) -> None:
    db = tmp_path / "trades.db"
    _make_trade_db(db, open_positions=0)
    conn = sqlite3.connect(str(db))
    assert vrt.check_zero_open_positions_or_entries_paused(conn) == "zero_open_positions"


def test_open_positions_without_entries_paused_refuses(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "trades.db"
    _make_trade_db(db, open_positions=2)
    conn = sqlite3.connect(str(db))
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    with pytest.raises(vrt.PreconditionError, match="open position"):
        vrt.check_zero_open_positions_or_entries_paused(conn)


def test_open_positions_with_entries_paused_passes_with_warning(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "trades.db"
    _make_trade_db(db, open_positions=2)
    conn = sqlite3.connect(str(db))
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: True
    )
    result = vrt.check_zero_open_positions_or_entries_paused(conn)
    assert "entries_paused" in result
    assert "writer-plane fence" in result


# ---------------------------------------------------------------------------
# Backup manifest
# ---------------------------------------------------------------------------


def test_backup_manifest_missing_file_refuses(tmp_path: Path) -> None:
    with pytest.raises(vrt.PreconditionError, match="not found"):
        vrt.check_backup_manifest(
            tmp_path / "nonexistent.json", db_path=tmp_path / "zeus_trades.db", max_age_hours=24
        )


def test_backup_manifest_too_old_refuses(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _make_backup_manifest(
        manifest_path, db_name="zeus_trades.db", created_at=datetime.now(timezone.utc) - timedelta(hours=48)
    )
    with pytest.raises(vrt.PreconditionError, match="older than"):
        vrt.check_backup_manifest(
            manifest_path, db_path=tmp_path / "zeus_trades.db", max_age_hours=24
        )


def test_backup_manifest_wrong_db_refuses(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _make_backup_manifest(
        manifest_path, db_name="zeus-world.db", created_at=datetime.now(timezone.utc)
    )
    with pytest.raises(vrt.PreconditionError, match="no entry for"):
        vrt.check_backup_manifest(
            manifest_path, db_path=tmp_path / "zeus_trades.db", max_age_hours=24
        )


def test_backup_manifest_unverified_refuses(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _make_backup_manifest(
        manifest_path, db_name="zeus_trades.db", created_at=datetime.now(timezone.utc), ok=False
    )
    with pytest.raises(vrt.PreconditionError, match="did not verify ok"):
        vrt.check_backup_manifest(
            manifest_path, db_path=tmp_path / "zeus_trades.db", max_age_hours=24
        )


def test_backup_manifest_fresh_and_verified_passes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _make_backup_manifest(
        manifest_path, db_name="zeus_trades.db", created_at=datetime.now(timezone.utc)
    )
    match = vrt.check_backup_manifest(
        manifest_path, db_path=tmp_path / "zeus_trades.db", max_age_hours=24
    )
    assert match["db"] == "zeus_trades.db"


# ---------------------------------------------------------------------------
# Source integrity
# ---------------------------------------------------------------------------


def test_source_integrity_ok_passes(tmp_path: Path) -> None:
    db = tmp_path / "trades.db"
    _make_trade_db(db)
    conn = sqlite3.connect(str(db))
    vrt.check_source_integrity(conn)  # must not raise


def test_source_integrity_failure_refuses(tmp_path: Path) -> None:
    db = tmp_path / "trades.db"
    _make_trade_db(db, decision_log_rows=200)
    # Truncate off the back half of the file, chopping through real b-tree
    # pages -- more robust than monkeypatching a C-extension type, and a
    # single corrupted byte doesn't reliably trip integrity_check depending
    # on which region it lands in.
    size = db.stat().st_size
    with open(db, "r+b") as f:
        f.truncate(size // 2)
    conn = sqlite3.connect(str(db))
    with pytest.raises(vrt.PreconditionError, match="integrity_check"):
        vrt.check_source_integrity(conn)


# ---------------------------------------------------------------------------
# Writer-plane fence
# ---------------------------------------------------------------------------


def test_writer_plane_fence_requires_flag() -> None:
    with pytest.raises(vrt.PreconditionError, match="requires the writer plane fenced"):
        vrt.assert_writer_plane_fenced(False)


def test_writer_plane_fence_passes_with_flag_and_no_live_processes(monkeypatch) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    vrt.assert_writer_plane_fenced(True)  # must not raise


# ---------------------------------------------------------------------------
# Full --vacuum-into flow (tiny disposable fixture files only)
# ---------------------------------------------------------------------------


def test_vacuum_into_full_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "source" / "zeus_trades.db"
    source.parent.mkdir()
    _make_trade_db(source, open_positions=0, decision_log_rows=800, bloat_and_delete_rows=1600)
    dest = tmp_path / "dest" / "zeus_trades_compact.db"

    receipt = vrt.run_vacuum_into(
        db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24
    )

    assert dest.exists()
    assert receipt["dest_integrity_check"] == "ok"
    assert receipt["dest_auto_vacuum_mode"] == "incremental"
    assert receipt["source_row_counts"]["decision_log"] == 800
    assert Path(receipt["receipt_path"]).exists()

    dest_conn = sqlite3.connect(str(dest))
    assert dest_conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert dest_conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 800


def test_vacuum_into_refuses_if_dest_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source)
    dest = tmp_path / "dest.db"
    dest.write_text("already here")

    with pytest.raises(vrt.PreconditionError, match="already exists"):
        vrt.run_vacuum_into(db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24)


def test_vacuum_into_refuses_on_open_positions_without_pause(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source, open_positions=3)
    dest = tmp_path / "dest.db"

    with pytest.raises(vrt.PreconditionError, match="open position"):
        vrt.run_vacuum_into(db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24)


# ---------------------------------------------------------------------------
# Full --swap flow
# ---------------------------------------------------------------------------


def test_swap_requires_a_prior_vacuum_into_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source)
    dest = tmp_path / "dest.db"
    dest.write_text("no receipt for this file")

    with pytest.raises(vrt.PreconditionError, match="no vacuum_reset receipt"):
        vrt.run_swap(
            db_path=source, dest=dest, operator_confirms_fenced=True,
            backup_manifest=None, backup_max_age_hours=24,
        )


def test_swap_full_flow_replaces_live_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source, open_positions=0, decision_log_rows=300, bloat_and_delete_rows=600)
    original_sha = vrt._sha256_file(source)
    dest = tmp_path / "compact.db"

    vrt.run_vacuum_into(db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24)
    dest_sha_before_swap = vrt._sha256_file(dest)

    result = vrt.run_swap(
        db_path=source, dest=dest, operator_confirms_fenced=True,
        backup_manifest=None, backup_max_age_hours=24,
    )

    assert result["post_swap_integrity_check"] == "ok"
    # The live path now holds the compacted content, not the original.
    assert vrt._sha256_file(source) == dest_sha_before_swap
    assert vrt._sha256_file(source) != original_sha
    # A pre-swap backup of the original file was preserved.
    backup_path = Path(result["pre_swap_backup_path"])
    assert backup_path.exists()
    assert vrt._sha256_file(backup_path) == original_sha
    # The compacted content is readable and has the right row count.
    live_conn = sqlite3.connect(str(source))
    assert live_conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 300


def test_quiescent_wal_mode_after_close_swaps_and_backs_up_main_only(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source, decision_log_rows=300, bloat_and_delete_rows=600)
    wal_conn = sqlite3.connect(str(source))
    assert wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    wal_conn.execute(
        "UPDATE decision_log SET payload='quiescent-wal-mode' "
        "WHERE id=(SELECT MIN(id) FROM decision_log)"
    )
    wal_conn.commit()
    wal_conn.close()
    source_wal = Path(str(source) + "-wal")
    source_shm = Path(str(source) + "-shm")
    assert not source_wal.exists() and not source_shm.exists()
    original_sha = vrt._sha256_file(source)
    dest = tmp_path / "compact.db"

    vrt.run_vacuum_into(
        db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24
    )
    captured_sidecars = {
        suffix: Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm")
    }
    result = vrt.run_swap(
        db_path=source,
        dest=dest,
        operator_confirms_fenced=True,
        backup_manifest=None,
        backup_max_age_hours=24,
    )

    backup = Path(result["pre_swap_backup_path"])
    assert backup.exists() and vrt._sha256_file(backup) == original_sha
    for suffix, existed_at_capture in captured_sidecars.items():
        assert Path(str(backup) + suffix).exists() is existed_at_capture
    assert not source_wal.exists() and not source_shm.exists()
    live_conn = sqlite3.connect(str(source))
    try:
        assert live_conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 300
    finally:
        live_conn.close()


def test_vacuum_into_refuses_real_second_connection_update_during_phase2(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source, decision_log_rows=300, bloat_and_delete_rows=600)
    dest = tmp_path / "compact.db"
    real_connect = vrt.sqlite3.connect
    trace_seen: list[str] = []
    writer_updates: list[int] = []

    def connect_with_phase2_writer(database, *args, **kwargs):
        conn = real_connect(database, *args, **kwargs)
        if database == f"file:{source}?mode=ro":
            def trace(statement: str) -> None:
                if not statement.lstrip().upper().startswith("VACUUM INTO"):
                    return
                trace_seen.append(statement)
                writer = real_connect(str(source))
                try:
                    changed = writer.execute(
                        "UPDATE decision_log SET payload='phase2-writer-update' "
                        "WHERE id=(SELECT MIN(id) FROM decision_log)"
                    )
                    writer.commit()
                    writer_updates.append(changed.rowcount)
                finally:
                    writer.close()

            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(vrt.sqlite3, "connect", connect_with_phase2_writer)
    with pytest.raises(vrt.PreconditionError, match="(?i)source changed"):
        vrt.run_vacuum_into(
            db_path=source,
            dest=dest,
            backup_manifest=None,
            backup_max_age_hours=24,
        )

    assert len(trace_seen) == 1
    assert writer_updates == [1]
    assert dest.exists()
    assert not dest.with_suffix(dest.suffix + ".vacuum_reset_receipt.json").exists()


def test_swap_refuses_without_operator_confirms_fenced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.control.control_plane.is_entries_paused", lambda: False
    )
    source = tmp_path / "zeus_trades.db"
    _make_trade_db(source, decision_log_rows=200, bloat_and_delete_rows=400)
    dest = tmp_path / "compact.db"
    vrt.run_vacuum_into(db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24)

    with pytest.raises(vrt.PreconditionError, match="writer plane fenced"):
        vrt.run_swap(
            db_path=source, dest=dest, operator_confirms_fenced=False,
            backup_manifest=None, backup_max_age_hours=24,
        )
    # Nothing was touched.
    assert source.exists()


def _prepare_vacuum_candidate(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source" / "zeus_trades.db"
    source.parent.mkdir()
    _make_trade_db(source, decision_log_rows=300, bloat_and_delete_rows=600)
    dest = tmp_path / "candidate" / "zeus_trades_compact.db"
    vrt.run_vacuum_into(
        db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24
    )
    return source, dest


def _content_snapshot(path: Path) -> tuple[tuple[object, ...], ...]:
    conn = sqlite3.connect(str(path))
    try:
        return tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT id, payload FROM decision_log ORDER BY id"
            ).fetchall()
        )
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["update", "insert", "delete", "wal_update"])
def test_swap_refuses_source_mutation_after_candidate_generation(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    source_inode = source.stat().st_ino
    dest_inode = dest.stat().st_ino
    candidate_content = _content_snapshot(dest)
    wal_conn = None
    try:
        if mutation == "update":
            conn = sqlite3.connect(str(source))
            original_payload = conn.execute(
                "SELECT payload FROM decision_log WHERE id=(SELECT MIN(id) FROM decision_log)"
            ).fetchone()[0]
            changed = conn.execute(
                "UPDATE decision_log SET payload=? "
                "WHERE id=(SELECT MIN(id) FROM decision_log)",
                ("updated-after-candidate".ljust(len(original_payload), "!"),),
            )
            assert changed.rowcount == 1
            conn.commit()
            conn.close()
        elif mutation == "insert":
            conn = sqlite3.connect(str(source))
            inserted = conn.execute(
                "INSERT INTO decision_log(mode, payload) VALUES ('settlement', 'inserted-after-candidate')"
            )
            assert inserted.rowcount == 1
            conn.commit()
            conn.close()
        elif mutation == "delete":
            conn = sqlite3.connect(str(source))
            changed = conn.execute(
                "DELETE FROM decision_log WHERE id=(SELECT MIN(id) FROM decision_log)"
            )
            assert changed.rowcount == 1
            conn.commit()
            conn.close()
        elif mutation == "wal_update":
            wal_conn = sqlite3.connect(str(source))
            assert wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            changed = wal_conn.execute(
                "UPDATE decision_log SET payload='wal-update-after-candidate' "
                "WHERE id=(SELECT MIN(id) FROM decision_log)"
            )
            assert changed.rowcount == 1
            wal_conn.commit()
        else:
            raise AssertionError(mutation)

        expected_source_content = _content_snapshot(source)
        with pytest.raises(vrt.PreconditionError, match="(?i)source|receipt"):
            vrt.run_swap(
                db_path=source,
                dest=dest,
                operator_confirms_fenced=True,
                backup_manifest=None,
                backup_max_age_hours=24,
            )

        assert source.exists() and source.stat().st_ino == source_inode
        assert dest.exists() and dest.stat().st_ino == dest_inode
        assert _content_snapshot(source) == expected_source_content
        assert _content_snapshot(dest) == candidate_content
    finally:
        if wal_conn is not None:
            wal_conn.close()


def test_swap_refuses_same_path_replacement_with_unchanged_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    source_bytes = source.read_bytes()
    source_inode = source.stat().st_ino
    candidate_inode = dest.stat().st_ino
    replacement = source.with_name("replacement.db")
    replacement.write_bytes(source_bytes)
    os.replace(replacement, source)
    assert source.stat().st_ino != source_inode

    with pytest.raises(vrt.PreconditionError, match="(?i)source|content|path"):
        vrt.run_swap(
            db_path=source,
            dest=dest,
            operator_confirms_fenced=True,
            backup_manifest=None,
            backup_max_age_hours=24,
        )

    assert source.exists() and source.stat().st_ino != source_inode
    assert source.read_bytes() == source_bytes
    assert dest.exists() and dest.stat().st_ino == candidate_inode


def test_swap_refuses_candidate_when_source_path_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    other_source = tmp_path / "other-source.db"
    _make_trade_db(other_source, decision_log_rows=300, bloat_and_delete_rows=600)
    other_inode = other_source.stat().st_ino
    other_content = _content_snapshot(other_source)
    candidate_inode = dest.stat().st_ino
    candidate_content = _content_snapshot(dest)

    with pytest.raises(vrt.PreconditionError, match="(?i)source|receipt"):
        vrt.run_swap(
            db_path=other_source,
            dest=dest,
            operator_confirms_fenced=True,
            backup_manifest=None,
            backup_max_age_hours=24,
        )

    assert source.exists()
    assert other_source.exists() and other_source.stat().st_ino == other_inode
    assert dest.exists() and dest.stat().st_ino == candidate_inode
    assert _content_snapshot(other_source) == other_content
    assert _content_snapshot(dest) == candidate_content


def test_swap_refuses_old_receipt_without_source_content_binding(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    receipt_path = dest.with_suffix(dest.suffix + ".vacuum_reset_receipt.json")
    receipt = json.loads(receipt_path.read_text())
    for key in list(receipt):
        lowered = key.lower()
        if "source" in lowered and any(
            marker in lowered
            for marker in ("sha", "hash", "digest", "binding", "fingerprint", "content")
        ):
            receipt.pop(key)
    receipt_path.write_text(json.dumps(receipt))
    source_inode = source.stat().st_ino
    candidate_inode = dest.stat().st_ino
    source_content = _content_snapshot(source)
    candidate_content = _content_snapshot(dest)

    with pytest.raises(vrt.PreconditionError, match="(?i)source|receipt"):
        vrt.run_swap(
            db_path=source,
            dest=dest,
            operator_confirms_fenced=True,
            backup_manifest=None,
            backup_max_age_hours=24,
        )

    assert source.exists() and source.stat().st_ino == source_inode
    assert dest.exists() and dest.stat().st_ino == candidate_inode
    assert _content_snapshot(source) == source_content
    assert _content_snapshot(dest) == candidate_content


def test_swap_refuses_source_wal_and_preserves_candidate_without_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    source_inode = source.stat().st_ino
    candidate_inode = dest.stat().st_ino
    source_wal = Path(str(source) + "-wal")
    source_shm = Path(str(source) + "-shm")
    candidate_wal = Path(str(dest) + "-wal")
    candidate_shm = Path(str(dest) + "-shm")
    wal_conn = sqlite3.connect(str(source))
    try:
        assert wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        changed = wal_conn.execute(
            "UPDATE decision_log SET payload='wal-sidecar-update' "
            "WHERE id=(SELECT MIN(id) FROM decision_log)"
        )
        assert changed.rowcount == 1
        wal_conn.commit()
        assert source_wal.exists() and source_shm.exists()
        assert not candidate_wal.exists() and not candidate_shm.exists()

        with pytest.raises(vrt.PreconditionError, match="(?i)source|receipt"):
            vrt.run_swap(
                db_path=source,
                dest=dest,
                operator_confirms_fenced=True,
                backup_manifest=None,
                backup_max_age_hours=24,
            )

        assert source.exists() and source.stat().st_ino == source_inode
        assert source_wal.exists() and source_shm.exists()
        assert dest.exists() and dest.stat().st_ino == candidate_inode
        assert not candidate_wal.exists() and not candidate_shm.exists()
    finally:
        wal_conn.close()


def test_swap_failure_after_source_group_rename_restores_main_and_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source = tmp_path / "source" / "zeus_trades.db"
    source.parent.mkdir()
    _make_trade_db(source, decision_log_rows=300, bloat_and_delete_rows=600)
    wal_conn = sqlite3.connect(str(source))
    assert wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    wal_conn.execute(
        "UPDATE decision_log SET payload='wal-before-candidate' "
        "WHERE id=(SELECT MIN(id) FROM decision_log)"
    )
    wal_conn.commit()
    dest = tmp_path / "candidate" / "zeus_trades_compact.db"
    vrt.run_vacuum_into(
        db_path=source, dest=dest, backup_manifest=None, backup_max_age_hours=24
    )
    source_inode = source.stat().st_ino
    candidate_inode = dest.stat().st_ino
    source_wal = Path(str(source) + "-wal")
    source_shm = Path(str(source) + "-shm")
    candidate_wal = Path(str(dest) + "-wal")
    candidate_shm = Path(str(dest) + "-shm")
    rename_calls: list[tuple[Path, Path]] = []
    move_calls: list[tuple[Path, Path]] = []
    try:
        assert source_wal.exists() and source_shm.exists()

        real_rename = vrt.os.rename
        real_move = vrt.shutil.move

        def track_rename(src, dst):
            rename_calls.append((Path(src), Path(dst)))
            return real_rename(src, dst)

        def fail_candidate_move(src, dst):
            move_calls.append((Path(src), Path(dst)))
            if Path(src) == dest:
                raise OSError("injected candidate move failure")
            return real_move(src, dst)

        monkeypatch.setattr(vrt.os, "rename", track_rename)
        monkeypatch.setattr(vrt.shutil, "move", fail_candidate_move)

        with pytest.raises(OSError, match="injected candidate move failure"):
            vrt.run_swap(
                db_path=source,
                dest=dest,
                operator_confirms_fenced=True,
                backup_manifest=None,
                backup_max_age_hours=24,
            )

        moved_sidecars = {path for path, _ in rename_calls + move_calls}
        assert source_wal in moved_sidecars and source_shm in moved_sidecars
        assert source.exists() and source.stat().st_ino == source_inode
        assert source_wal.exists() and source_shm.exists()
        assert dest.exists() and dest.stat().st_ino == candidate_inode
        assert not candidate_wal.exists() and not candidate_shm.exists()
    finally:
        wal_conn.close()


def test_swap_verify_failure_removes_new_live_sidecars_when_source_had_none(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed post-move verifier must not leave sidecars on the restored source."""
    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    source_inode = source.stat().st_ino
    candidate_inode = dest.stat().st_ino
    source_wal = Path(str(source) + "-wal")
    source_shm = Path(str(source) + "-shm")
    assert not source_wal.exists() and not source_shm.exists()

    real_connect = vrt.sqlite3.connect
    verify_connect_calls = 0

    def inject_verify_failure(database, *args, **kwargs):
        nonlocal verify_connect_calls
        conn = real_connect(database, *args, **kwargs)
        if isinstance(database, str) and database == f"file:{source}?mode=ro":
            verify_connect_calls += 1
            if verify_connect_calls == 2:
                conn.close()
                source_wal.write_bytes(b"new live wal")
                source_shm.write_bytes(b"new live shm")
                raise OSError("injected post-move verification failure")
        return conn

    monkeypatch.setattr(vrt.sqlite3, "connect", inject_verify_failure)
    with pytest.raises(OSError, match="injected post-move verification failure"):
        vrt.run_swap(
            db_path=source,
            dest=dest,
            operator_confirms_fenced=True,
            backup_manifest=None,
            backup_max_age_hours=24,
        )

    assert verify_connect_calls == 2
    assert source.exists() and source.stat().st_ino == source_inode
    assert dest.exists() and dest.stat().st_ino == candidate_inode
    assert not source_wal.exists() and not source_shm.exists()
    assert not Path(str(dest) + "-wal").exists()
    assert not Path(str(dest) + "-shm").exists()


def test_swap_refuses_when_shared_cutover_lease_is_held(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    from src.state.db_writer_lock import cutover_lease_path

    monkeypatch.setenv(vrt._SKIP_PROCESS_CHECK_ENV_VAR, "1")
    source, dest = _prepare_vacuum_candidate(tmp_path)
    lease = cutover_lease_path(source.resolve())
    source_inode = source.stat().st_ino
    candidate_inode = dest.stat().st_ino
    with lease.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(vrt.PreconditionError, match="(?i)lease|cutover"):
            vrt.run_swap(
                db_path=source,
                dest=dest,
                operator_confirms_fenced=True,
                backup_manifest=None,
                backup_max_age_hours=24,
            )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert source.exists() and source.stat().st_ino == source_inode
    assert dest.exists() and dest.stat().st_ino == candidate_inode
