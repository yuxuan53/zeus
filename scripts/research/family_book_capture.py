"""Continuous full-family order-book capture for Polymarket weather markets (research).

Standalone: imports nothing from ``src``. Subscribes to the public CLOB market
websocket for every YES and NO token of every active weather family (all cities in
``config/cities.json``, target dates today..today+N) and writes top-of-book changes,
rate-limited depth, and feed health into a bounded SQLite file of its own.

Why it exists: ``executable_market_snapshots`` is a family-burst record written only
where the daemon considered trading (bursts ~34 min apart); ``token_price_log`` holds
only Zeus's own tokens. Any book hypothesis faster than a burst is unobservable on
those tables. This feed is the measurement precondition, not a trading path.

Launch (operator decision; not launchd-managed here):
    .venv/bin/python scripts/research/family_book_capture.py --db state/research/family_books.db
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
USER_AGENT = "zeus-research-family-book-capture/0.1"
SLUG_PREFIXES = ("highest-temperature-in-{city}-on-{date}", "lowest-temperature-in-{city}-on-{date}")
SUBSCRIBE_BATCH = 500  # Polymarket: at most 500 asset ids per subscribe message
UNIVERSE_REFRESH_S = 30 * 60
DEPTH_MIN_INTERVAL_S = 60.0
HEALTH_INTERVAL_S = 60.0
RETENTION_TICK_S = 3600.0

log = logging.getLogger("family_book_capture")


# --------------------------------------------------------------------------- universe

def gamma_slug(city_slug: str, target: date, prefix: str) -> str:
    """``highest-temperature-in-qingdao-on-september-7-2026`` (no zero padding)."""
    return prefix.format(city=city_slug, date=f"{target.strftime('%B').lower()}-{target.day}-{target.year}")


def city_slugs(cities_json: Path, only: set[str] | None = None) -> list[str]:
    data = json.loads(cities_json.read_text())
    out: list[str] = []
    for city in data["cities"]:
        if only and city["name"] not in only and not (set(city.get("slug_names") or []) & only):
            continue
        out.extend(city.get("slug_names") or [])
    return sorted(set(out))


def tokens_from_event(event: dict) -> list[dict]:
    """Flatten one gamma event into per-token rows: token_id, condition_id, event_slug, outcome_label, bin."""
    rows: list[dict] = []
    event_slug = str(event.get("slug") or "")
    for market in event.get("markets") or []:
        if not market.get("enableOrderBook", True):
            continue
        if market.get("closed") or market.get("archived"):
            continue
        try:
            token_ids = json.loads(market.get("clobTokenIds") or "[]")
            outcomes = json.loads(market.get("outcomes") or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if len(token_ids) != 2 or len(outcomes) != 2:
            continue
        for token_id, outcome in zip(token_ids, outcomes, strict=True):
            rows.append(
                {
                    "token_id": str(token_id),
                    "condition_id": str(market.get("conditionId") or ""),
                    "event_slug": event_slug,
                    "market_slug": str(market.get("slug") or ""),
                    "outcome_label": str(outcome).upper(),
                    "bin_label": str(market.get("groupItemTitle") or market.get("question") or ""),
                    "min_tick_size": market.get("orderPriceMinTickSize"),
                    "min_order_size": market.get("orderMinSize"),
                }
            )
    return rows


def resolve_universe(cities: list[str], *, days_ahead: int, fetch, today: date | None = None) -> dict[str, dict]:
    """token_id -> metadata for every active family. ``fetch(slug) -> list[event] | None``."""
    today = today or datetime.now(UTC).date()
    universe: dict[str, dict] = {}
    for offset in range(days_ahead + 1):
        target = today + timedelta(days=offset)
        for city in cities:
            for prefix in SLUG_PREFIXES:
                slug = gamma_slug(city, target, prefix)
                events = fetch(slug)
                for event in events or []:
                    if event.get("closed"):
                        continue
                    for row in tokens_from_event(event):
                        row["city_slug"] = city
                        row["target_date"] = target.isoformat()
                        row["metric"] = "high" if prefix.startswith("highest") else "low"
                        universe[row["token_id"]] = row
    return universe


def make_gamma_fetch(session, cache_ttl_s: float = 60.0):
    cache: dict[str, tuple[float, list]] = {}

    def fetch(slug: str) -> list | None:
        now = time.monotonic()
        hit = cache.get(slug)
        if hit and now - hit[0] < cache_ttl_s:
            return hit[1]
        try:
            resp = session.get(GAMMA_EVENTS, params={"slug": slug}, timeout=20.0)
            if resp.status_code != 200:
                log.warning("gamma %s -> HTTP %s", slug, resp.status_code)
                return None
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - research feed keeps running
            log.warning("gamma %s failed: %s", slug, exc)
            return None
        events = data if isinstance(data, list) else []
        cache[slug] = (now, events)
        return events

    return fetch


# --------------------------------------------------------------------------- frames

def top_of_book(levels: list | None, *, best: str) -> tuple[float | None, float | None]:
    """(price, size) at the best level; bids take the max price, asks the min."""
    parsed: list[tuple[float, float]] = []
    for level in levels or []:
        try:
            if isinstance(level, dict):
                parsed.append((float(level["price"]), float(level.get("size") or 0.0)))
            else:
                parsed.append((float(level[0]), float(level[1])))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    if not parsed:
        return None, None
    return max(parsed) if best == "bid" else min(parsed)


def top_hash(best_bid, best_ask, bid_size, ask_size) -> str:
    return hashlib.sha1(f"{best_bid}|{best_ask}|{bid_size}|{ask_size}".encode()).hexdigest()[:16]


def parse_frame(message: dict, *, prior_top: dict[str, dict]) -> list[dict]:
    """Normalise one websocket message into zero or more top-of-book updates.

    ``book``: full snapshot (bids/asks). ``price_change``: list of per-asset changes carrying
    best_bid/best_ask (sizes unknown -> carried from the prior top when the price is unchanged,
    else None). ``last_trade_price`` and ``tick_size_change`` carry no book state and are skipped.
    """
    event_type = str(message.get("event_type") or message.get("type") or "")
    exchange_ts = message.get("timestamp")
    out: list[dict] = []
    if event_type == "book":
        asset = str(message.get("asset_id") or "")
        if not asset:
            return out
        bid, bid_size = top_of_book(message.get("bids"), best="bid")
        ask, ask_size = top_of_book(message.get("asks"), best="ask")
        out.append(
            {
                "token_id": asset,
                "exchange_ts": exchange_ts,
                "best_bid": bid,
                "best_ask": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "book_hash": str(message.get("hash") or ""),
                "levels": {"bids": message.get("bids") or [], "asks": message.get("asks") or []},
            }
        )
        return out
    if event_type == "price_change":
        changes = message.get("price_changes") or message.get("changes") or []
        if not changes and message.get("asset_id"):
            changes = [message]
        for change in changes:
            if not isinstance(change, dict):
                continue
            asset = str(change.get("asset_id") or message.get("asset_id") or "")
            if not asset:
                continue
            prior = prior_top.get(asset) or {}
            bid = _float(change.get("best_bid"))
            ask = _float(change.get("best_ask"))
            out.append(
                {
                    "token_id": asset,
                    "exchange_ts": change.get("timestamp") or exchange_ts,
                    "best_bid": bid,
                    "best_ask": ask,
                    "bid_size": prior.get("bid_size") if prior.get("best_bid") == bid else None,
                    "ask_size": prior.get("ask_size") if prior.get("best_ask") == ask else None,
                    "book_hash": str(change.get("hash") or ""),
                    "levels": None,
                }
            )
    return out


def _float(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- sink

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_top (
    token_id TEXT NOT NULL, condition_id TEXT, event_slug TEXT, outcome_label TEXT,
    received_at_utc TEXT NOT NULL, received_monotonic REAL NOT NULL, exchange_ts TEXT,
    best_bid REAL, best_ask REAL, bid_size REAL, ask_size REAL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_top_token_time ON book_top(token_id, received_at_utc);
CREATE INDEX IF NOT EXISTS idx_book_top_time ON book_top(received_at_utc);
CREATE TABLE IF NOT EXISTS book_depth (
    token_id TEXT NOT NULL, received_at_utc TEXT NOT NULL, exchange_ts TEXT, levels_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_depth_token_time ON book_depth(token_id, received_at_utc);
CREATE TABLE IF NOT EXISTS feed_health (
    received_at_utc TEXT NOT NULL, connected INTEGER NOT NULL, subscribed_tokens INTEGER NOT NULL,
    msgs_per_min REAL NOT NULL, reconnects INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS token_meta (
    token_id TEXT PRIMARY KEY, condition_id TEXT, event_slug TEXT, market_slug TEXT, outcome_label TEXT,
    bin_label TEXT, city_slug TEXT, target_date TEXT, metric TEXT, min_tick_size TEXT, min_order_size TEXT,
    first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL
);
"""


class Sink:
    """Single-writer SQLite sink: change-deduped top-of-book, rate-limited depth, retention."""

    def __init__(self, path: Path, *, retain_days: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA journal_size_limit=67108864")
        self.conn.executescript(SCHEMA)
        self.retain_days = retain_days
        self.last_top: dict[str, dict] = {}
        self.last_depth_at: dict[str, float] = {}
        self.rows_written = 0

    def upsert_meta(self, universe: dict[str, dict], now_iso: str) -> None:
        self.conn.execute("BEGIN")
        for token_id, m in universe.items():
            self.conn.execute(
                """INSERT INTO token_meta(token_id, condition_id, event_slug, market_slug, outcome_label, bin_label,
                   city_slug, target_date, metric, min_tick_size, min_order_size, first_seen_utc, last_seen_utc)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(token_id) DO UPDATE SET last_seen_utc=excluded.last_seen_utc""",
                (
                    token_id, m.get("condition_id"), m.get("event_slug"), m.get("market_slug"), m.get("outcome_label"),
                    m.get("bin_label"), m.get("city_slug"), m.get("target_date"), m.get("metric"),
                    str(m.get("min_tick_size")), str(m.get("min_order_size")), now_iso, now_iso,
                ),
            )
        self.conn.execute("COMMIT")

    def write_update(self, update: dict, meta: dict | None, *, now_iso: str, now_mono: float) -> bool:
        """Persist one normalised update; returns True when the top-of-book changed."""
        token = update["token_id"]
        h = top_hash(update["best_bid"], update["best_ask"], update["bid_size"], update["ask_size"])
        prior = self.last_top.get(token)
        changed = prior is None or prior["hash"] != h
        if changed:
            self.conn.execute(
                """INSERT INTO book_top(token_id, condition_id, event_slug, outcome_label, received_at_utc,
                   received_monotonic, exchange_ts, best_bid, best_ask, bid_size, ask_size, hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    token, (meta or {}).get("condition_id"), (meta or {}).get("event_slug"),
                    (meta or {}).get("outcome_label"), now_iso, now_mono, _ts_iso(update.get("exchange_ts")),
                    update["best_bid"], update["best_ask"], update["bid_size"], update["ask_size"], h,
                ),
            )
            self.rows_written += 1
            self.last_top[token] = {
                "hash": h, "best_bid": update["best_bid"], "best_ask": update["best_ask"],
                "bid_size": update["bid_size"], "ask_size": update["ask_size"],
            }
        levels = update.get("levels")
        if levels is not None and changed and now_mono - self.last_depth_at.get(token, -1e9) >= DEPTH_MIN_INTERVAL_S:
            self.conn.execute(
                "INSERT INTO book_depth(token_id, received_at_utc, exchange_ts, levels_json) VALUES(?,?,?,?)",
                (token, now_iso, _ts_iso(update.get("exchange_ts")), json.dumps(levels, separators=(",", ":"))),
            )
            self.last_depth_at[token] = now_mono
            self.rows_written += 1
        return changed

    def write_health(self, *, now_iso: str, connected: bool, subscribed: int, msgs_per_min: float, reconnects: int) -> None:
        self.conn.execute(
            "INSERT INTO feed_health(received_at_utc, connected, subscribed_tokens, msgs_per_min, reconnects) VALUES(?,?,?,?,?)",
            (now_iso, int(connected), subscribed, msgs_per_min, reconnects),
        )

    def apply_retention(self, now: datetime) -> int:
        cutoff = (now - timedelta(days=self.retain_days)).isoformat()
        total = 0
        for table in ("book_top", "book_depth", "feed_health"):
            cur = self.conn.execute(f"DELETE FROM {table} WHERE received_at_utc < ?", (cutoff,))
            total += cur.rowcount
        return total


def _ts_iso(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and "T" in value:
        return value
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw > 10_000_000_000:
        return datetime.fromtimestamp(raw / 1000.0, tz=UTC).isoformat()
    return datetime.fromtimestamp(raw, tz=UTC).isoformat()


# --------------------------------------------------------------------------- loop

async def run(args: argparse.Namespace) -> None:
    import httpx
    import websockets

    sink = Sink(Path(args.db), retain_days=args.retain_days)
    session = httpx.Client(headers={"User-Agent": USER_AGENT})
    fetch = make_gamma_fetch(session)
    cities = city_slugs(REPO / "config" / "cities.json", set(args.cities) if args.cities else None)
    universe: dict[str, dict] = {}
    reconnects = 0
    msgs_window: list[float] = []
    stop_at = time.monotonic() + args.duration_s if args.duration_s else None
    last_universe = -1e9
    last_health = time.monotonic()
    last_retention = time.monotonic()
    steady_rows_at: int | None = None
    steady_t0: float | None = None
    t_start = time.monotonic()

    while stop_at is None or time.monotonic() < stop_at:
        if time.monotonic() - last_universe >= UNIVERSE_REFRESH_S:
            new_universe = await asyncio.to_thread(resolve_universe, cities, days_ahead=args.days_ahead, fetch=fetch)
            if new_universe:
                universe = new_universe
                sink.upsert_meta(universe, datetime.now(UTC).isoformat())
                log.info("universe: %d tokens across %d families", len(universe), len({m['event_slug'] for m in universe.values()}))
            last_universe = time.monotonic()
        if not universe:
            log.warning("empty universe; retrying in 60 s")
            await asyncio.sleep(60)
            last_universe = -1e9
            continue
        subscribed = sorted(universe)
        try:
            async with websockets.connect(
                WS_ENDPOINT, ping_interval=30, ping_timeout=90, max_size=16 * 1024 * 1024,
                additional_headers={"User-Agent": USER_AGENT},
            ) as ws:
                for i in range(0, len(subscribed), SUBSCRIBE_BATCH):
                    await ws.send(json.dumps({"assets_ids": subscribed[i:i + SUBSCRIBE_BATCH], "type": "market"}))
                log.info("connected; subscribed %d tokens in %d message(s)", len(subscribed), (len(subscribed) + SUBSCRIBE_BATCH - 1) // SUBSCRIBE_BATCH)
                next_universe_at = last_universe + UNIVERSE_REFRESH_S
                connected_at_mono = time.monotonic()
                last_health = connected_at_mono
                steady_t0, steady_rows_at = None, None
                quiet_ticks = 0
                while stop_at is None or time.monotonic() < stop_at:
                    if time.monotonic() >= next_universe_at:
                        break  # reconnect with the refreshed universe
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        raw = None
                        quiet_ticks += 1
                        if quiet_ticks % 12 == 0:
                            log.info("socket quiet for %d s (rows=%d)", quiet_ticks * 5, sink.rows_written)
                    else:
                        quiet_ticks = 0
                    now_mono = time.monotonic()
                    now_iso = datetime.now(UTC).isoformat()
                    if raw is not None:
                        msgs_window.append(now_mono)
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            message = None
                        items = message if isinstance(message, list) else [message]
                        sink.conn.execute("BEGIN")
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            for update in parse_frame(item, prior_top=sink.last_top):
                                sink.write_update(update, universe.get(update["token_id"]), now_iso=now_iso, now_mono=now_mono)
                        sink.conn.execute("COMMIT")
                    if steady_t0 is None and now_mono - connected_at_mono >= 60.0:
                        steady_t0, steady_rows_at = now_mono, sink.rows_written  # after the subscribe snapshot burst
                    if now_mono - last_health >= HEALTH_INTERVAL_S:
                        msgs_window = [t for t in msgs_window if now_mono - t <= 60.0]
                        sink.write_health(now_iso=now_iso, connected=True, subscribed=len(subscribed), msgs_per_min=float(len(msgs_window)), reconnects=reconnects)
                        rows_per_day = _rows_per_day(sink.rows_written, steady_rows_at, steady_t0, now_mono)
                        log.info("health: msgs/min=%d rows=%d steady_rows/day=%s est_bytes/day≈%s MB", len(msgs_window), sink.rows_written, "n/a" if rows_per_day is None else f"{rows_per_day:.0f}", "n/a" if rows_per_day is None else f"{rows_per_day * 180 / 1e6:.1f}")
                        last_health = now_mono
                    if now_mono - last_retention >= RETENTION_TICK_S:
                        deleted = sink.apply_retention(datetime.now(UTC))
                        log.info("retention: deleted %d rows older than %d d", deleted, args.retain_days)
                        last_retention = now_mono
        except (OSError, websockets.exceptions.WebSocketException, asyncio.IncompleteReadError) as exc:
            reconnects += 1
            sink.write_health(now_iso=datetime.now(UTC).isoformat(), connected=False, subscribed=len(subscribed), msgs_per_min=0.0, reconnects=reconnects)
            delay = min(60.0, 2.0 * reconnects)
            log.warning("websocket dropped (%s); reconnect #%d in %.0f s", exc, reconnects, delay)
            await asyncio.sleep(delay)
    rows_per_day = _rows_per_day(sink.rows_written, steady_rows_at, steady_t0, time.monotonic())
    print(json.dumps({
        "tokens_subscribed": len(universe),
        "families": len({m["event_slug"] for m in universe.values()}),
        "rows_written": sink.rows_written,
        "msgs_per_min_last_window": len([t for t in msgs_window if time.monotonic() - t <= 60.0]),
        "steady_rows_per_day": None if rows_per_day is None else round(rows_per_day),
        "steady_db_bytes_per_day": None if rows_per_day is None else round(rows_per_day * 180),
        "elapsed_s": round(time.monotonic() - t_start),
        "reconnects": reconnects,
    }))


def _rows_per_day(rows_now: int, rows_at: int | None, t0: float | None, now: float) -> float | None:
    """Rows/day extrapolated from the steady window (after the post-subscribe snapshot burst)."""
    if rows_at is None or t0 is None or now - t0 < 30.0:
        return None
    return (rows_now - rows_at) / (now - t0) * 86400.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", default=str(REPO / "state" / "research" / "family_books.db"))
    parser.add_argument("--retain-days", type=int, default=21)
    parser.add_argument("--cities", nargs="*", default=None, help="restrict to these city names or slugs")
    parser.add_argument("--days-ahead", type=int, default=3)
    parser.add_argument("--duration-s", type=float, default=None, help="stop after N seconds (smoke runs)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
