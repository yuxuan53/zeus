# Source-family NOAA city-universe evidence

## Defect

On 2026-09-02, direct Gamma pagination returned 325 active weather-tagged
events. The live checkout admitted current daily-temperature events for only
six cities because 45 same-station contracts now named
`weather.gov/wrh/timeseries?site=<ICAO>` while `config/cities.json` still
declared `wu_icao`. Recent canonical `market_events`, venue commands, and fills
were therefore concentrated in Hong Kong, Istanbul, Moscow, Tel Aviv, Taipei,
and Jinan before probability ranking could compare the full universe.

The observed transition dates were reconstructed per city from consecutive
Gamma events: 17 cities changed on 2026-08-23 and 28 on 2026-08-24. Station
identity and settlement unit did not change.

## Current-source audit after patch

Executed from the packet worktree against the current Gamma API:

- 325 active weather-tagged events fetched.
- 235 active daily-temperature events parsed.
- 51 configured cities represented.
- 235/235 parsed events had source-contract status `MATCH`.
- Target dates were 2026-09-01 (35), 2026-09-02 (102), and 2026-09-03 (98).

This proves source-contract admission for the current city universe. It does
not prove positive expected value, fills, or realized capital gain.

## Transition and ingestion antibodies

- Market source-family checks are target-date scoped.
- Hourly writer/tier/source-role validation is target-date scoped.
- Daily tick, hole scanner, catch-up, WU/Ogimet backfills, and persistence ETL
  preserve WU rows before each transition and require NOAA/Ogimet afterward.
- NOAA/Ogimet station maps are derived from `cities.json`; there is no
  Istanbul/Moscow/Tel Aviv-only runtime registry.
- A single current AviationWeather batch returned 147 reports and covered all
  48 configured NOAA stations; live Day0 evidence is therefore available
  independently of the slow history mirror.
- Slow Ogimet daily/hourly work is sharded across 24 hourly runs, shares one
  21-second in-process request governor, and is excluded from the 15-minute
  fast tick. Catch-up is bounded and daily-rotated so one failing city cannot
  permanently hold the front of the queue.
- Ogimet raw temperatures remain recorded in Celsius provenance but are
  explicitly converted through the typed `Temperature` boundary before an
  F-settled city's executable daily value is written.
- Historical gap-fill and Day0 readers resolve the source family from the
  target date. A post-transition NOAA witness cannot authorize a
  pre-transition WU target (or the reverse).
- The held-position monitor passes the position target date through to that
  same source-priority resolver; it cannot silently query the current NOAA
  surface for a pre-transition WU position.

## Tests

- 494 affected source, transition, scanner, writer, ingest, and Day0 tests
  passed.
- Full Day0 fast-observation/reader slice: 175 passed, 1 failed. The same
  wall-clock-dependent Istanbul replay fails on the unmodified live checkout.
- Full K2/backfill slice: 90 passed, 6 failed. The same six tests fail on the
  unmodified live checkout with identical causes (pre-existing payload-revision,
  removed K2 scheduler, and model-source-map assertions); no new failure was
  introduced by this packet.
- Six focused antibodies for unit conversion, request governance, target-date
  gap routing, and target-date Day0 source identity all pass.
- The monitor target-date propagation antibody passes. Independent read-only
  review found no remaining P0/P1 finding; the stale city-count prose in the
  pre-existing invalid `architecture/source_rationale.yaml` remains P2 drift
  and is not used as runtime truth.

## Remaining deployment proof

After landing and daemon reload, verify current `market_events`, posterior
materialization, family eligibility, global selection receipts, venue facts,
and portfolio capital truth. Do not infer capital advantage from the 51-city
admission result alone.

## Post-deploy queue starvation defect

After the source-family hotfix landed as `274fe3a4`, the current-target planner
expanded to 51 cities and fetched every missing same-cycle Open-Meteo anchor
manifest. The live `anchor_missing_scope_count` fell from 199 to zero. Austin,
Atlanta, and Toronto then had complete materialization requests, but those
files remained queued while the one-second priority lane repeatedly spent both
of its bounded request slots on held Day0 revisions from the original six-city
set.

The request interleaver reserved its second slot only for a family already in
the global-auction scope. A never-priced family cannot enter that scope until
its first q exists, creating a circular gate: first q required auction scope,
while auction scope required first q. The hotfix keeps the held-capital slot
and uses the second slot for the strongest non-held priority request when no
existing global-auction request is available. The queue is already filtered to
priority-owned work at that boundary, so background traffic does not gain the
reserved slot.

Focused tests prove both the existing held-plus-global ordering and the new
held-plus-first-q ordering. The broader materialization slices introduce no new
failure; two queue tests and one bounded-read test fail identically on the
unmodified live checkout.

Immediately after reload, the widened seed universe exposed a third break:
every priority callback spent 12-18 seconds in seed preparation and returned
`REPLACEMENT_LIVE_MATERIALIZATION_CLAIM_DEFERRED_READ_DEADLINE` before it could
claim any prepared request. The priority callback now runs the existing
request-only path first (`seed_limit=0`) and bridges two seeds only when that
path truthfully returns `NO_REQUESTS`. This preserves bounded seed progress
while preventing seed planning from becoming a mutex in front of complete q
requests. Seven focused scheduler/interleave tests pass; the wider scheduler
slice's single failure reproduces unchanged on live.
