# source_family_noaa_city_universe -- Plan

Date: 2026-09-02
Branch: `hotfix/materializer-new-city-fairness`
Status: active

## Background

Live Gamma audit on 2026-09-02 found 235 active daily-temperature events
covering 51 configured cities. Forty-five cities now resolve through
`weather.gov/wrh/timeseries?site=<ICAO>` while Zeus still configured them as
`wu_icao`. The station identity did not change, but the provider-family gate
correctly rejected every mismatched event before persistence. Consequently the
canonical current universe contained only six matching cities and all recent
venue commands were concentrated there.

This packet repairs the provider-family migration without weakening source
validation or forcing economically negative orders.

## Scope

_See sibling scope.yaml for machine-readable scope._

## Deliverables

- Record the 51-city live resolver audit and the six-city canonical funnel.
- Migrate only currently evidenced same-station `wu_icao -> noaa` cities.
- Generalize the Ogimet/NOAA source registry from three hardcoded cities to all
  configured NOAA stations, preserving per-city source tags and units.
- Preserve HKO, Jinan, Taipei, and currently inactive city contracts.
- Prove new-city Gamma events parse, persist, receive forecast/Day0 data, and
  enter the global family selection funnel.
- Keep one bounded priority-materialization slot available to non-held
  first-posterior work even while held Day0 revisions continuously arrive.
- Claim prepared priority requests before scanning the expanded seed backlog;
  bridge a bounded seed tranche only when no actionable request remains.
- Re-run the strict current-regime capital evaluator; city coverage is not
  itself capital-advantage proof.

## Verification

- `pytest -q tests/test_tier_resolver.py tests/test_cities_config_authoritative.py`
- `pytest -q tests/test_market_scanner_provenance.py tests/test_scanner_slug_pattern.py`
- `pytest -q tests/test_k2_live_ingestion_relationships.py tests/test_backfill_scripts_match_live_config.py`
- `pytest -q tests/test_day0_fast_obs_lane.py tests/test_day0_observation_reader.py`
- Gamma live audit: 51 active cities, zero source-contract mismatches.
- Canonical DB recheck after deploy: more than six cities in current
  `market_events`, current posteriors, family receipts, and executable snapshot
  candidates.
- Strict evaluator and `omx performance-goal checkpoint`.
