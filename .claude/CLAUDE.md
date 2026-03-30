# CLAUDE.md — Zeus

## What This Is

Zeus is a Polymarket weather prediction market trading engine. It replaces Rainstorm (retired — data assets inherited, code discarded). Zeus is a **market microstructure exploitation engine**, not a weather forecasting system that happens to trade.

## Edge Thesis (ranked by durability)

1. **Favorite-Longshot Bias** — Retail overpays for low-probability shoulder bins, underpays for high-probability center bins
2. **Opening Price Inertia** — First liquidity provider sets sticky prices; 6-24h post-open has largest model-vs-market gap
3. **Bin Boundary Discretization** — WU settles on integer-rounded °F; continuous models miss probability discontinuities at bin edges

## Architecture Overview

- **Signal**: ECMWF ENS 51-member ensemble → Monte Carlo with instrument noise (σ=0.5°F) → P_raw per bin
- **Cross-check**: GFS 31-member ensemble (conflict detection only, never blended)
- **Calibration**: Platt scaling per bucket (cluster × season × lead_band, 72 buckets), bootstrap parameter uncertainty
- **Edge**: Double-bootstrap CI (σ_ensemble + σ_parameter + σ_instrument); CI_lower > 0 required
- **Sizing**: Quarter-Kelly with dynamic multiplier, portfolio heat / drawdown / correlation constraints
- **Execution**: Limit orders only, VWMP fair value (not mid-price), toxicity-aware cancel

## Data Foundation

Inherited from Rainstorm (SQLite):
- 1,634 settlements, 4,410 IEM ASOS daily, 6,520 NOAA GHCND daily, 105K Meteostat hourly
- 285K token price log rows, 14.9K market events, 53.6K ladder backfill, 71 WU PWS city-days

Settlement authority: Polymarket result > WU PWS > IEM ASOS + offset > Meteostat

## Key Design Decisions

- **VWMP everywhere** — all edge calculations use volume-weighted micro-price, never mid-price
- **WU integer rounding** — always simulate the full settlement chain (atmosphere → NWP → sensor → METAR → WU integer °F)
- **Maturity gates** — calibration bucket with n<15 uses P_raw directly with 3× edge threshold
- **Hierarchical fallback** — city+season+lead → cluster+season+lead → season+lead → global → uncalibrated
- **Model conflict = skip** — ECMWF vs GFS CONFLICT (KL > 0.15) → skip market entirely
- **No re-evaluation after entry** — once ENTERED, only exit triggers are checked (no second-guessing)

## Cities

NYC, Chicago, Seattle, Atlanta, Dallas, Miami, LA, SF (US). London, Paris (Europe).

Clusters: US-Northeast, US-Midwest, US-Southeast, US-SouthCentral, US-Pacific, Europe

## Portfolio Constraints

| Limit | Value |
|-------|-------|
| Max single position | 10% bankroll |
| Max portfolio heat | 50% |
| Max correlated exposure | 25% |
| Max city exposure | 20% |
| Max region exposure | 35% |
| Daily loss halt | 8% |
| Weekly loss halt | 15% |
| Max drawdown halt | 20% |
| Min order | $1.00 |

## Discovery Modes

- **Mode A: Opening Hunt** — every 30 min, scan markets <24h old
- **Mode B: Update Reaction** — 4×/day after ECMWF 00Z/12Z arrival, check exits + scan existing markets
- **Mode C: Day0 Capture** — every 15 min for markets <6h to resolution, observation + residual ENS

## Exit Triggers (exhaustive)

SETTLEMENT, EDGE_REVERSAL (2 consecutive ENS runs), STOP_LOSS (>40% cost basis), RISK_HALT, NWS_EXTREME, EXPIRY_EXIT (<4h + unprofitable), BIMODAL_SHIFT

**NOT triggers**: edge shrinking (still positive), model soft-disagree, price fluctuations within stop, slightly different P on new ENS run.

## Common Commands

```bash
cd workspace-venus/zeus
source .venv/bin/activate          # once venv exists
python -m pytest tests/            # run all tests
python -m pytest tests/test_X.py   # single file
```

## Conventions

- **State writes**: atomic — write tmp, then `os.replace()` to target
- **Time**: Chicago local time primary, ET secondary. Never expose UTC to users.
- **Language**: English
- **Spec**: `project level docs/ZEUS_SPEC.md` is the authoritative design document. Code that contradicts it requires explicit justification.
- **Four research documents** (quant research, architecture blueprint, market microstructure, statistical methodology) in `project level docs/` are design authority — all decisions trace to them.

## Testing

- Tests in `tests/` mirror `src/` structure
- Run from project root with venv activated
- Use real data fixtures where possible, mock only external API calls
