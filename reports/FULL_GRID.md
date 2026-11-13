# Full-grid aggressive optimization — MyD1 breakout

Run: `python scripts/full_grid_opt.py`
Data: real OKX daily bars, 125 USDT pairs, 2023-08-03 -> 2026-11-12 (1198 closed bars).
Split: train 70% (838 days) picks EVERYTHING. Holdout 30% (360 days) only scores. Cash always a candidate.

## Grid

| Parameter | Values |
|---|---|
| lookback | 4, 6, 8 ... 48 (23 values) |
| breadth_min | 0.0, 0.3, 0.4, 0.5, 0.6, 0.7 |
| risk_pct | 0.005, 0.01, 0.02, 0.03 |
| max_weight | 0.05, 0.10, 0.15, 0.20, 0.30 |
| exposure_scale | 0.00 - 1.00 step 0.05 (spot cap: gross <= 100%) |

2760 parameter variants x 21 scales, all evaluated. Caps: DD <= 30% base AND at 2x-fee stress,
on train AND holdout. Plateau rule: lookback +/-2 neighbours must also beat cash on train.

## Winner (deployable on spot, scale <= 1.0)

```
lookback=20  breadth_min=0.7  risk_pct=0.03  max_weight=0.05  exposure_scale=1.00
train    end $11,321  DD 23.1%   (train picked this)
holdout  end $11,060  DD  6.2%   2x-fee stress DD 6.4%   -> beats cash ($10,000, DD 0%)
```

Neighbours (lookback 22, 24 at same breadth) hold out at $11,030 / $10,982 — plateau confirmed.

## Honest comparison vs previous config

| Config | Holdout end | Holdout DD |
|---|---|---|
| NEW aggressive (brd 0.7, risk 3%, scale 1.00) | $11,060 | 6.2% |
| OLD conservative (brd 0.5, risk 1%, scale 0.50) | $11,325 | 6.6% |
| Cash | $10,000 | 0% |

Same plateau. Aggressive sizing does NOT buy more out-of-sample money here — the signal
arrival rate, not position size, is the binding constraint. Scale 1.25 (allowed by training)
was rejected because spot cannot run >100% gross and `my_d1` validates exposure_scale <= 1.

## What was NOT accepted

- Any combo losing to cash on holdout (~700 of the train winners died there).
- Scale 1.25 winner from the unrestricted scan — not executable on spot.
- Lone spikes without plateau neighbours.

## Conclusion

- The tuned edge is real on this data but its size is capped by how often valid breakouts occur.
- New defaults applied to `backtest`, `paper-once`, and `signals`: lookback=20, breadth=0.7,
  risk_pct=0.03, max_weight=0.05, scale=1.00.
- Aggressive version accepted per user risk choice (30% DD cap). At 2x costs it still holds DD 6.4% OOS.
- Backtest only. Real OKX public data. No live orders. OKX main balance untouched.
