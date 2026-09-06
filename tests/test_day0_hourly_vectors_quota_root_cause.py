# Created: 2026-09-05
# Purpose: Regression tests for the round-3 quota root-cause fixes in
#   src/data/day0_hourly_vectors.py: a monotone per-model provider-run HWM pin (Open-
#   Meteo's meta.json is served from more than one replica; replicas have been observed
#   disagreeing about which run is current, and about run_availability_time for the SAME
#   run), plus the shared single-runs payload cache (src/data/bayes_precision_fusion_
#   download.py) that makes an all-or-nothing incomplete-bundle retry cheap.
"""TDD for the day0 provider-run HWM pin and the incomplete-bundle retry cost."""
from __future__ import annotations

from datetime import UTC, date, datetime

import src.data.day0_hourly_vectors as day0
from src.data.day0_hourly_vectors import Day0ProviderRunHwm


def _hwm(model: str, init: datetime, avail: datetime) -> Day0ProviderRunHwm:
    return Day0ProviderRunHwm(
        model=model, run_initialisation_time=init, run_availability_time=avail
    )


def test_provider_run_hwm_pin_ignores_stale_replica_older_run(monkeypatch) -> None:
    """Alternating 12Z/18Z probes for the SAME model: once 18Z is accepted, a later
    probe reporting 12Z again (a stale meta.json replica) must never displace it."""
    day0._DAY0_PROVIDER_RUN_HWM_PIN.clear()
    monkeypatch.setattr(day0, "_day0_provider_run_hwm_pin_persistence_enabled", lambda: False)

    run_12z = datetime(2026, 9, 5, 12, tzinfo=UTC)
    run_18z = datetime(2026, 9, 5, 18, tzinfo=UTC)
    avail_12z = datetime(2026, 9, 5, 18, 19, 59, tzinfo=UTC)
    avail_18z = datetime(2026, 9, 6, 0, 27, 39, tzinfo=UTC)

    sequence = [run_18z, run_12z, run_18z, run_12z, run_18z, run_18z]
    for run in sequence:
        avail = avail_18z if run == run_18z else avail_12z
        probed = {"ecmwf_ifs": _hwm("ecmwf_ifs", run, avail)}
        pinned = day0._apply_day0_provider_run_hwm_pin(probed)
        assert pinned["ecmwf_ifs"].run_initialisation_time == run_18z, (
            "the pin must never regress to an older run once the newer one is accepted"
        )


def test_provider_run_hwm_pin_keeps_earliest_availability_for_same_run(monkeypatch) -> None:
    """Same run, two different availability replicas: the pin keeps the EARLIEST
    availability_time seen so a later replica cannot push public-usability backwards."""
    day0._DAY0_PROVIDER_RUN_HWM_PIN.clear()
    monkeypatch.setattr(day0, "_day0_provider_run_hwm_pin_persistence_enabled", lambda: False)

    run = datetime(2026, 9, 5, 18, tzinfo=UTC)
    early_avail = datetime(2026, 9, 6, 0, 27, 39, tzinfo=UTC)
    late_avail = datetime(2026, 9, 6, 0, 54, 11, tzinfo=UTC)

    first = day0._apply_day0_provider_run_hwm_pin(
        {"ecmwf_ifs": _hwm("ecmwf_ifs", run, early_avail)}
    )
    assert first["ecmwf_ifs"].run_availability_time == early_avail

    second = day0._apply_day0_provider_run_hwm_pin(
        {"ecmwf_ifs": _hwm("ecmwf_ifs", run, late_avail)}
    )
    assert second["ecmwf_ifs"].run_availability_time == early_avail

    third = day0._apply_day0_provider_run_hwm_pin(
        {"ecmwf_ifs": _hwm("ecmwf_ifs", run, early_avail)}
    )
    assert third["ecmwf_ifs"].run_availability_time == early_avail


def test_provider_run_hwm_pin_advances_on_genuinely_newer_run(monkeypatch) -> None:
    day0._DAY0_PROVIDER_RUN_HWM_PIN.clear()
    monkeypatch.setattr(day0, "_day0_provider_run_hwm_pin_persistence_enabled", lambda: False)

    run_18z = datetime(2026, 9, 5, 18, tzinfo=UTC)
    run_00z = datetime(2026, 9, 6, 0, tzinfo=UTC)
    avail_18z = datetime(2026, 9, 6, 0, 27, 39, tzinfo=UTC)
    avail_00z = datetime(2026, 9, 6, 6, 30, 0, tzinfo=UTC)

    day0._apply_day0_provider_run_hwm_pin({"ecmwf_ifs": _hwm("ecmwf_ifs", run_18z, avail_18z)})
    advanced = day0._apply_day0_provider_run_hwm_pin(
        {"ecmwf_ifs": _hwm("ecmwf_ifs", run_00z, avail_00z)}
    )
    assert advanced["ecmwf_ifs"].run_initialisation_time == run_00z
    assert advanced["ecmwf_ifs"].run_availability_time == avail_00z


def test_current_provider_bundle_already_persisted_ignores_availability_replica_skew(
    monkeypatch,
) -> None:
    """The exact defect: _current_provider_bundle_already_persisted compared the full
    (init, availability) pair, so a persisted bundle for a run already captured failed
    this check -- and re-triggered a full re-fetch -- whenever the HWM probe's
    availability_time differed from the persisted row's, even for the identical run."""
    run = datetime(2026, 9, 5, 18, tzinfo=UTC)
    persisted_avail = datetime(2026, 9, 6, 0, 27, 39, tzinfo=UTC)
    hwm_avail = datetime(2026, 9, 6, 0, 54, 11, tzinfo=UTC)  # a different replica

    persisted_meta = {
        "model": "ecmwf_ifs",
        "provider": "openmeteo",
        "provider_source_cycle_time_utc": run.isoformat(),
        "provider_source_available_at_utc": persisted_avail.isoformat(),
    }

    class _Vector:
        def __init__(self, model: str, meta: dict) -> None:
            self.model = model
            import json as _json

            self.source_run_meta_json = _json.dumps(meta)

    def _fake_read_freshest(**kwargs):
        return [_Vector("ecmwf_ifs", persisted_meta)]

    monkeypatch.setattr(day0, "read_freshest_day0_hourly_vectors", _fake_read_freshest)

    class _FakeConn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.state.db.get_forecasts_connection_read_only", lambda: _FakeConn()
    )

    required_hwm = {"ecmwf_ifs": _hwm("ecmwf_ifs", run, hwm_avail)}
    already_persisted = day0._current_provider_bundle_already_persisted(
        city="Singapore",
        target_dates=("2026-09-06",),
        expected_models=("ecmwf_ifs",),
        required_hwm=required_hwm,
        decision_time=datetime(2026, 9, 6, 1, 0, tzinfo=UTC),
        remaining_window_starts={"2026-09-06": datetime(2026, 9, 6, 0, 0, tzinfo=UTC)},
    )
    assert already_persisted is True, (
        "a bundle already persisted for the SAME run must not be re-fetched merely "
        "because a different meta.json replica reports a different availability_time"
    )


def test_incomplete_bundle_retry_reuses_cached_models_via_shared_payload_cache(
    monkeypatch,
) -> None:
    """QUOTA round 3: day0's all-or-nothing bundle (fetch_day0_hourly_vectors) discards
    every already-fetched model's payload when a LATER model in the loop fails, and the
    45s-cadence incomplete-bundle retry re-requests the whole bundle. The shared payload
    cache in _fetch_single_runs_hourly_payloads_batched must serve the models that
    already succeeded from cache on the retry: a 4-model bundle where one model fails on
    the first pass and succeeds on the second must issue 4 + 1 HTTP calls total, not 8."""
    import src.data.bayes_precision_fusion_download as dl
    import src.data.openmeteo_client as client

    dl._SINGLE_RUNS_PAYLOAD_CACHE.clear()
    models = ["ecmwf_ifs", "icon_global", "ukmo_global_deterministic_10km", "gem_hrdps_continental"]
    location = (1.35019, 103.994003, "Asia/Singapore", (date(2026, 9, 6),))
    run = datetime(2026, 9, 5, 18, tzinfo=UTC)

    call_count = {"n": 0}
    fail_once_for = "gem_hrdps_continental"
    failed_already = {"done": False}

    def _payload() -> dict:
        return {
            "hourly": {
                "time": ["2026-09-06T00:00", "2026-09-06T21:00"],
                "temperature_2m": [24.0, 30.0],
            },
            "hourly_units": {"temperature_2m": "°C"},
        }

    def _fetch(_url, params, **kwargs):
        call_count["n"] += 1
        model_param = str(params.get("models", ""))
        if (
            fail_once_for in model_param
            and not failed_already["done"]
        ):
            failed_already["done"] = True
            raise RuntimeError("synthetic transport failure for gem_hrdps_continental")
        return _payload()

    monkeypatch.setattr(client, "fetch", _fetch)

    def _one_bundle_pass() -> list[str]:
        """Mirror _day0_exact_run_payloads' per-model loop shape: one model at a time,
        the whole pass raises (and its results are discarded) if any model fails."""
        succeeded: list[str] = []
        for model in models:
            dl._fetch_single_runs_hourly_payloads_batched(
                models=[model], locations=[location], run=run, forecast_hours=72,
            )
            succeeded.append(model)
        return succeeded

    # Pass 1: gem_hrdps_continental fails; its own AND the bundle's other successes
    # are discarded by the all-or-nothing contract (matches fetch_day0_hourly_vectors'
    # fail-soft try/except around _day0_exact_run_payloads).
    try:
        _one_bundle_pass()
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "the synthetic failure must propagate like a real transport error"
    assert call_count["n"] == 4, "pass 1 attempts every model exactly once"

    # Pass 2 (retry): the 3 models that already succeeded must be cache hits; only
    # gem_hrdps_continental (now succeeding) issues a real HTTP call.
    succeeded = _one_bundle_pass()
    assert succeeded == models
    assert call_count["n"] == 5, "pass 2 must add exactly ONE new HTTP call, not 4"
