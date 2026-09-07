from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = REPO / "scripts" / "research" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cap = _load("family_book_capture")
cov = _load("family_book_coverage")


def test_gamma_slug_matches_polymarket_pattern():
    assert cap.gamma_slug("qingdao", date(2026, 9, 7), cap.SLUG_PREFIXES[0]) == "highest-temperature-in-qingdao-on-september-7-2026"
    assert cap.gamma_slug("hong-kong", date(2026, 12, 25), cap.SLUG_PREFIXES[1]) == "lowest-temperature-in-hong-kong-on-december-25-2026"


def test_city_slugs_reads_registry_and_filters(tmp_path):
    reg = tmp_path / "cities.json"
    reg.write_text(json.dumps({"cities": [
        {"name": "Qingdao", "slug_names": ["qingdao"]},
        {"name": "Hong Kong", "slug_names": ["hong-kong", "hongkong"]},
    ]}))
    assert cap.city_slugs(reg) == ["hong-kong", "hongkong", "qingdao"]
    assert cap.city_slugs(reg, {"Qingdao"}) == ["qingdao"]
    assert cap.city_slugs(reg, {"hongkong"}) == ["hong-kong", "hongkong"]


def _event(slug: str, n_bins: int = 2, closed: bool = False) -> dict:
    markets = []
    for i in range(n_bins):
        markets.append({
            "conditionId": f"0xcond{i}", "slug": f"{slug}-{i}", "groupItemTitle": f"{20+i}°C",
            "clobTokenIds": json.dumps([f"yes{i}-{slug}", f"no{i}-{slug}"]), "outcomes": json.dumps(["Yes", "No"]),
            "enableOrderBook": True, "closed": closed, "archived": False,
            "orderPriceMinTickSize": 0.001, "orderMinSize": 5,
        })
    return {"slug": slug, "closed": closed, "markets": markets}


def test_resolve_universe_covers_both_sides_every_bin_and_dates():
    calls: list[str] = []

    def fetch(slug: str):
        calls.append(slug)
        return [_event(slug, n_bins=3)] if "qingdao" in slug else []

    uni = cap.resolve_universe(["qingdao", "tokyo"], days_ahead=1, fetch=fetch, today=date(2026, 9, 6))
    assert len(calls) == 2 * 2 * 3  # cities × prefixes × dates (yesterday, today, tomorrow)
    assert len(uni) == 3 * 2 * 3 * 2  # dates × metrics × bins × sides
    row = uni["no1-highest-temperature-in-qingdao-on-september-6-2026"]
    assert row["outcome_label"] == "NO" and row["condition_id"] == "0xcond1" and row["city_slug"] == "qingdao"
    assert {r["metric"] for r in uni.values()} == {"high", "low"}
    assert {r["target_date"] for r in uni.values()} == {"2026-09-05", "2026-09-06", "2026-09-07"}


def test_resolve_universe_skips_closed_markets_and_bad_token_maps():
    ev = _event("highest-temperature-in-x-on-september-6-2026", n_bins=2)
    ev["markets"][0]["closed"] = True
    ev["markets"][1]["clobTokenIds"] = "[\"only-one\"]"
    assert cap.resolve_universe(["x"], days_ahead=0, fetch=lambda s: [ev], today=date(2026, 9, 6)) == {}


def _fresh_state():
    return {"ladders": {}, "initialised": {}, "epoch": 1, "stats": {"dropped_uninitialised": 0}}


def test_parse_book_frame_takes_best_levels_and_sizes():
    msg = {"event_type": "book", "asset_id": "t1", "timestamp": "1788704332292", "hash": "abc",
           "bids": [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "3"}],
           "asks": [{"price": "0.50", "size": "7"}, {"price": "0.45", "size": "2"}]}
    st = _fresh_state()
    (u,) = cap.parse_frame(msg, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    assert (u["best_bid"], u["bid_size"], u["best_ask"], u["ask_size"]) == (0.42, 3.0, 0.45, 2.0)
    assert u["is_snapshot"] is True
    assert {(lvl["price"], lvl["size"]) for lvl in u["levels"]["asks"]} == {(0.5, 7.0), (0.45, 2.0)}
    assert cap._ts_iso(u["exchange_ts"]).startswith("2026-09-")
    assert st["initialised"]["t1"] == 1


def test_price_change_same_best_price_size_update_produces_new_top():
    """Defect 1a: same-best-price bid size 100 -> 40 produces a new top row with bid_size 40."""
    st = _fresh_state()
    book = {"event_type": "book", "asset_id": "t1", "timestamp": "1", "hash": "h",
            "bids": [{"price": "0.40", "size": "100"}], "asks": [{"price": "0.50", "size": "5"}]}
    cap.parse_frame(book, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    change = {"event_type": "price_change", "timestamp": 2,
              "price_changes": [{"asset_id": "t1", "price": "0.40", "size": "40", "side": "BUY", "hash": "h2"}]}
    (u,) = cap.parse_frame(change, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    assert u["is_snapshot"] is False
    assert (u["best_bid"], u["bid_size"]) == (0.40, 40.0)


def test_price_change_deeper_level_leaves_top_unchanged(tmp_path):
    """Defect 1b: a deeper-level change with unchanged top produces no top row, but the next
    depth checkpoint (from the maintained ladder) reflects it."""
    st = _fresh_state()
    book = {"event_type": "book", "asset_id": "t1", "timestamp": "1", "hash": "h",
            "bids": [{"price": "0.40", "size": "100"}, {"price": "0.35", "size": "5"}],
            "asks": [{"price": "0.50", "size": "5"}]}
    (u0,) = cap.parse_frame(book, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    s = cap.Sink(tmp_path / "books.db", retain_days=21)
    now_iso = datetime.now(UTC).isoformat()
    assert s.write_top(u0, None, now_iso=now_iso, now_mono=0.0, epoch=1) is True
    s.write_depth("t1", u0["levels"], now_iso=now_iso, exchange_ts=None, epoch=1)
    s.last_depth_at["t1"] = 0.0

    change = {"event_type": "price_change", "timestamp": 2,
              "price_changes": [{"asset_id": "t1", "price": "0.35", "size": "8", "side": "BUY", "hash": "h2"}]}
    (u1,) = cap.parse_frame(change, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    assert (u1["best_bid"], u1["bid_size"]) == (0.40, 100.0)  # top unchanged
    assert s.write_top(u1, None, now_iso=now_iso, now_mono=1.0, epoch=1) is False
    assert s.conn.execute("SELECT COUNT(*) FROM book_top").fetchone()[0] == 1

    n = s.maybe_checkpoint_depth(st["ladders"], now_iso=now_iso, now_mono=61.0, epoch=1)
    assert n == 1
    levels_json = s.conn.execute("SELECT levels_json FROM book_depth ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    levels = json.loads(levels_json)
    assert {"price": 0.35, "size": 8.0} in levels["bids"]


def test_price_change_size_zero_removes_level_and_promotes_next_best():
    """Defect 1c: size 0 removes the level and the next best becomes top."""
    st = _fresh_state()
    book = {"event_type": "book", "asset_id": "t1", "timestamp": "1", "hash": "h",
            "bids": [{"price": "0.40", "size": "10"}, {"price": "0.38", "size": "5"}],
            "asks": [{"price": "0.50", "size": "5"}]}
    cap.parse_frame(book, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    change = {"event_type": "price_change", "timestamp": 2,
              "price_changes": [{"asset_id": "t1", "price": "0.40", "size": "0", "side": "BUY", "hash": "h2"}]}
    (u,) = cap.parse_frame(change, ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"])
    assert (u["best_bid"], u["bid_size"]) == (0.38, 5.0)
    assert 0.40 not in st["ladders"]["t1"].bids


def test_price_change_dropped_when_token_not_initialised_in_current_epoch():
    """Defect 2: after a simulated reconnect (epoch bump) a price_change for a token with no
    fresh book snapshot this epoch writes nothing and is counted as dropped."""
    st = _fresh_state()
    book = {"event_type": "book", "asset_id": "t1", "timestamp": "1", "hash": "h",
            "bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.50", "size": "5"}]}
    cap.parse_frame(book, ladders=st["ladders"], initialised=st["initialised"], epoch=1, stats=st["stats"])
    # simulated reconnect: epoch bumps, no fresh book for t1 yet in the new epoch
    change = {"event_type": "price_change", "timestamp": 2,
              "price_changes": [{"asset_id": "t1", "price": "0.41", "size": "20", "side": "BUY", "hash": "h2"}]}
    out = cap.parse_frame(change, ladders=st["ladders"], initialised=st["initialised"], epoch=2, stats=st["stats"])
    assert out == []
    assert st["stats"]["dropped_uninitialised"] == 1


def test_parse_frame_skips_trade_and_tick_events():
    st = _fresh_state()
    assert cap.parse_frame({"event_type": "last_trade_price", "asset_id": "t1", "price": "0.4"},
                            ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"]) == []
    assert cap.parse_frame({"event_type": "tick_size_change", "asset_id": "t1"},
                            ladders=st["ladders"], initialised=st["initialised"], epoch=st["epoch"]) == []


def _sink(tmp_path, retain_days=21):
    return cap.Sink(tmp_path / "books.db", retain_days=retain_days)


def test_sink_writes_only_on_top_change(tmp_path):
    s = _sink(tmp_path)
    upd = {"token_id": "t1", "exchange_ts": None, "best_bid": 0.4, "best_ask": 0.5, "bid_size": 1.0, "ask_size": 1.0,
           "book_hash": "", "levels": None}
    now = datetime.now(UTC).isoformat()
    assert s.write_top(upd, None, now_iso=now, now_mono=0.0, epoch=1) is True
    assert s.write_top(dict(upd), None, now_iso=now, now_mono=1.0, epoch=1) is False
    upd2 = dict(upd, best_ask=0.51)
    assert s.write_top(upd2, None, now_iso=now, now_mono=2.0, epoch=1) is True
    upd3 = dict(upd, bid_size=2.0)  # size-only change at an unchanged price must also count as a top change
    assert s.write_top(upd3, None, now_iso=now, now_mono=3.0, epoch=1) is True
    assert s.conn.execute("SELECT COUNT(*) FROM book_top").fetchone()[0] == 3
    assert s.conn.execute("SELECT connection_epoch FROM book_top LIMIT 1").fetchone()[0] == 1


def test_sink_depth_checkpoint_is_time_gated_per_token_independent_of_top(tmp_path):
    """Defect 1: book_depth checkpoints run on a 60 s clock per token from the maintained
    ladder, independent of whether the top-of-book changed."""
    s = _sink(tmp_path)
    ladder = cap.Ladder()
    ladder.load_book([{"price": "0.40", "size": "10"}], [{"price": "0.50", "size": "5"}])
    ladders = {"t1": ladder}
    now_iso = datetime.now(UTC).isoformat()
    assert s.maybe_checkpoint_depth(ladders, now_iso=now_iso, now_mono=0.0, epoch=1) == 1
    assert s.maybe_checkpoint_depth(ladders, now_iso=now_iso, now_mono=30.0, epoch=1) == 0  # inside the 60 s window
    assert s.maybe_checkpoint_depth(ladders, now_iso=now_iso, now_mono=61.0, epoch=1) == 0  # window elapsed, ladder unchanged: no row
    next(iter(ladders.values())).apply_change("BUY", 0.41, 5.0)
    assert s.maybe_checkpoint_depth(ladders, now_iso=now_iso, now_mono=62.0, epoch=1) == 1  # changed after the window: row
    assert s.conn.execute("SELECT COUNT(*) FROM book_depth").fetchone()[0] == 2
    assert s.conn.execute("SELECT connection_epoch FROM book_depth LIMIT 1").fetchone()[0] == 1


def test_sink_retention_deletes_only_old_rows(tmp_path):
    s = _sink(tmp_path, retain_days=1)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    new = datetime.now(UTC).isoformat()
    base = {"token_id": "t1", "exchange_ts": None, "best_bid": 0.4, "best_ask": 0.5, "bid_size": 1.0, "ask_size": 1.0, "book_hash": "", "levels": None}
    s.write_top(base, None, now_iso=old, now_mono=0.0, epoch=1)
    s.write_top(dict(base, best_ask=0.6), None, now_iso=new, now_mono=1.0, epoch=1)
    s.write_health(now_iso=old, connected=True, subscribed=1, msgs_per_min=1.0, reconnects=0)
    assert s.apply_retention(datetime.now(UTC)) == 2
    assert s.conn.execute("SELECT COUNT(*) FROM book_top").fetchone()[0] == 1
    assert s.conn.execute("SELECT COUNT(*) FROM feed_health").fetchone()[0] == 0


def test_coverage_census_reports_gaps_ask_only_and_validity_for_unchanged_quote(tmp_path):
    """Defect 3: an unchanged quote (token b never updates again) on a healthy connection
    must count as valid throughout, not degrade coverage after fresh_s."""
    s = _sink(tmp_path)
    t0 = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    s.upsert_meta({"a": {"event_slug": "fam", "outcome_label": "YES"}, "b": {"event_slug": "fam", "outcome_label": "NO"}}, t0.isoformat())
    book_a = {"token_id": "a", "exchange_ts": None, "best_bid": 0.40, "best_ask": 0.5, "bid_size": 1, "ask_size": 1, "book_hash": "", "levels": None}
    book_b = {"token_id": "b", "exchange_ts": None, "best_bid": None, "best_ask": 0.9, "bid_size": None, "ask_size": 1, "book_hash": "", "levels": None}
    s.write_top(book_a, {"event_slug": "fam"}, now_iso=t0.isoformat(), now_mono=0.0, epoch=1)
    s.write_top(book_b, {"event_slug": "fam"}, now_iso=t0.isoformat(), now_mono=0.0, epoch=1)
    for k in range(1, 6):  # token a keeps changing every 30 s; token b sends nothing more
        upd = dict(book_a, best_bid=0.40 + k * 0.01)
        s.write_top(upd, {"event_slug": "fam"}, now_iso=(t0 + timedelta(seconds=30 * k)).isoformat(), now_mono=30.0 * k, epoch=1)
    for k in range(6):  # feed stays connected throughout
        s.write_health(now_iso=(t0 + timedelta(seconds=30 * k)).isoformat(), connected=True, subscribed=2, msgs_per_min=7.0, reconnects=0)
    out = cov.census(str(tmp_path / "books.db"), grid_s=60.0)
    assert out["rows"] == 7 and out["tokens"] == 2 and out["families"] == 1
    assert out["ask_only_share"] == round(1 / 7, 3)
    assert out["median_change_gap_s"] == 30.0
    fam = out["per_family"]["fam"]
    assert fam["tokens"] == 2
    assert fam["all_fresh_fraction"] == 1.0
    assert out["health"]["msgs_per_min_avg"] == 7.0


def test_coverage_validity_drops_on_disconnect_and_zero_row_family_stays_in_denominator(tmp_path):
    """Defect 3: validity is gated by feed_health connected state, and families/tokens come
    from token_meta so a subscribed family with zero rows stays in the denominator at 0 coverage."""
    s = _sink(tmp_path)
    t0 = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    s.upsert_meta({
        "a": {"event_slug": "fam", "outcome_label": "YES"},
        "b": {"event_slug": "fam", "outcome_label": "NO"},
        "z": {"event_slug": "ghost-fam", "outcome_label": "YES"},  # subscribed, never sent a row
    }, t0.isoformat())
    book_a = {"token_id": "a", "exchange_ts": None, "best_bid": 0.40, "best_ask": 0.5, "bid_size": 1, "ask_size": 1, "book_hash": "", "levels": None}
    book_b = {"token_id": "b", "exchange_ts": None, "best_bid": 0.30, "best_ask": 0.4, "bid_size": 1, "ask_size": 1, "book_hash": "", "levels": None}
    s.write_top(book_a, {"event_slug": "fam"}, now_iso=t0.isoformat(), now_mono=0.0, epoch=1)
    s.write_top(book_b, {"event_slug": "fam"}, now_iso=t0.isoformat(), now_mono=0.0, epoch=1)
    s.write_health(now_iso=t0.isoformat(), connected=True, subscribed=2, msgs_per_min=1.0, reconnects=0)
    s.write_health(now_iso=(t0 + timedelta(seconds=60)).isoformat(), connected=False, subscribed=2, msgs_per_min=0.0, reconnects=1)
    s.write_health(now_iso=(t0 + timedelta(seconds=120)).isoformat(), connected=True, subscribed=2, msgs_per_min=1.0, reconnects=1)
    s.write_top(dict(book_a, best_bid=0.41), {"event_slug": "fam"}, now_iso=(t0 + timedelta(seconds=180)).isoformat(), now_mono=180.0, epoch=1)
    out = cov.census(str(tmp_path / "books.db"), grid_s=60.0)
    fam = out["per_family"]["fam"]
    assert 0.0 < fam["all_fresh_fraction"] < 1.0  # the disconnected interval drags coverage below 1.0
    ghost = out["per_family"]["ghost-fam"]
    assert ghost["tokens"] == 1 and ghost["tokens_with_rows"] == 0 and ghost["all_fresh_fraction"] == 0.0
