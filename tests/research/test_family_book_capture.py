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
    assert len(calls) == 2 * 2 * 2  # cities × prefixes × dates
    assert len(uni) == 2 * 2 * 3 * 2  # dates × metrics × bins × sides
    row = uni["no1-highest-temperature-in-qingdao-on-september-6-2026"]
    assert row["outcome_label"] == "NO" and row["condition_id"] == "0xcond1" and row["city_slug"] == "qingdao"
    assert {r["metric"] for r in uni.values()} == {"high", "low"}
    assert {r["target_date"] for r in uni.values()} == {"2026-09-06", "2026-09-07"}


def test_resolve_universe_skips_closed_markets_and_bad_token_maps():
    ev = _event("highest-temperature-in-x-on-september-6-2026", n_bins=2)
    ev["markets"][0]["closed"] = True
    ev["markets"][1]["clobTokenIds"] = "[\"only-one\"]"
    assert cap.resolve_universe(["x"], days_ahead=0, fetch=lambda s: [ev], today=date(2026, 9, 6)) == {}


def test_parse_book_frame_takes_best_levels_and_sizes():
    msg = {"event_type": "book", "asset_id": "t1", "timestamp": "1788704332292", "hash": "abc",
           "bids": [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "3"}],
           "asks": [{"price": "0.50", "size": "7"}, {"price": "0.45", "size": "2"}]}
    (u,) = cap.parse_frame(msg, prior_top={})
    assert (u["best_bid"], u["bid_size"], u["best_ask"], u["ask_size"]) == (0.42, 3.0, 0.45, 2.0)
    assert u["levels"]["asks"][0]["price"] == "0.50"
    assert cap._ts_iso(u["exchange_ts"]).startswith("2026-09-")


def test_parse_price_change_frame_carries_prior_size_only_when_price_unchanged():
    prior = {"t1": {"best_bid": 0.42, "best_ask": 0.45, "bid_size": 3.0, "ask_size": 2.0, "hash": "x"}}
    msg = {"event_type": "price_change", "timestamp": 1788704332292,
           "price_changes": [{"asset_id": "t1", "best_bid": "0.42", "best_ask": "0.46", "hash": "h2"}]}
    (u,) = cap.parse_frame(msg, prior_top=prior)
    assert u["best_bid"] == 0.42 and u["bid_size"] == 3.0
    assert u["best_ask"] == 0.46 and u["ask_size"] is None
    assert u["levels"] is None
    assert cap.parse_frame({"event_type": "last_trade_price", "asset_id": "t1", "price": "0.4"}, prior_top=prior) == []
    assert cap.parse_frame({"event_type": "tick_size_change", "asset_id": "t1"}, prior_top=prior) == []


def _sink(tmp_path, retain_days=21):
    return cap.Sink(tmp_path / "books.db", retain_days=retain_days)


def test_sink_writes_only_on_top_change_and_rate_limits_depth(tmp_path):
    s = _sink(tmp_path)
    upd = {"token_id": "t1", "exchange_ts": None, "best_bid": 0.4, "best_ask": 0.5, "bid_size": 1.0, "ask_size": 1.0,
           "book_hash": "", "levels": {"bids": [], "asks": []}}
    now = datetime.now(UTC).isoformat()
    assert s.write_update(upd, None, now_iso=now, now_mono=0.0) is True
    assert s.write_update(dict(upd), None, now_iso=now, now_mono=1.0) is False
    upd2 = dict(upd, best_ask=0.51)
    assert s.write_update(upd2, None, now_iso=now, now_mono=2.0) is True
    upd3 = dict(upd, best_ask=0.52)
    assert s.write_update(upd3, None, now_iso=now, now_mono=61.0) is True
    tops = s.conn.execute("SELECT COUNT(*) FROM book_top").fetchone()[0]
    depths = s.conn.execute("SELECT COUNT(*) FROM book_depth").fetchone()[0]
    assert tops == 3
    assert depths == 2  # t=0 and t=61; the t=2 change was inside the 60 s depth window


def test_sink_retention_deletes_only_old_rows(tmp_path):
    s = _sink(tmp_path, retain_days=1)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    new = datetime.now(UTC).isoformat()
    base = {"token_id": "t1", "exchange_ts": None, "best_bid": 0.4, "best_ask": 0.5, "bid_size": 1.0, "ask_size": 1.0, "book_hash": "", "levels": None}
    s.write_update(base, None, now_iso=old, now_mono=0.0)
    s.write_update(dict(base, best_ask=0.6), None, now_iso=new, now_mono=1.0)
    s.write_health(now_iso=old, connected=True, subscribed=1, msgs_per_min=1.0, reconnects=0)
    assert s.apply_retention(datetime.now(UTC)) == 2
    assert s.conn.execute("SELECT COUNT(*) FROM book_top").fetchone()[0] == 1
    assert s.conn.execute("SELECT COUNT(*) FROM feed_health").fetchone()[0] == 0


def test_coverage_census_reports_freshness_gaps_and_ask_only(tmp_path):
    s = _sink(tmp_path)
    t0 = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    s.upsert_meta({"a": {"event_slug": "fam", "outcome_label": "YES"}, "b": {"event_slug": "fam", "outcome_label": "NO"}}, t0.isoformat())
    for k in range(6):  # token a every 30 s, token b only at t0 -> family not all-fresh after 60 s
        s.write_update({"token_id": "a", "exchange_ts": None, "best_bid": 0.4 + k * 0.01, "best_ask": 0.5, "bid_size": 1, "ask_size": 1, "book_hash": "", "levels": None},
                       {"event_slug": "fam"}, now_iso=(t0 + timedelta(seconds=30 * k)).isoformat(), now_mono=30.0 * k)
    s.write_update({"token_id": "b", "exchange_ts": None, "best_bid": None, "best_ask": 0.9, "bid_size": None, "ask_size": 1, "book_hash": "", "levels": None},
                   {"event_slug": "fam"}, now_iso=t0.isoformat(), now_mono=0.0)
    s.write_health(now_iso=t0.isoformat(), connected=True, subscribed=2, msgs_per_min=7.0, reconnects=0)
    out = cov.census(str(tmp_path / "books.db"), fresh_s=60.0, grid_s=60.0)
    assert out["rows"] == 7 and out["tokens"] == 2 and out["families"] == 1
    assert out["ask_only_share"] == round(1 / 7, 3)
    assert out["median_change_gap_s"] == 30.0
    fam = out["per_family"]["fam"]
    assert fam["tokens"] == 2 and 0.0 < fam["all_fresh_fraction"] < 1.0
    assert out["health"]["msgs_per_min_avg"] == 7.0
