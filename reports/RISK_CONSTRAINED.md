# Risk-constrained result — 2026-11-12

Data: OKX public 1Dutc cache, 125 names, 2023-08-02 to 2026-11-11 (1198 days).
Rule: pick exposure on first 70% only. Score last 30% only.
Cap: max drawdown < 20% on base cost and on 2x fee+slippage.
Cash (scale = 0) is always allowed.

## Benchmarks (full window)

| Book | End equity | Max DD | PnL |
|------|------------|--------|-----|
| Cash | 10000 | 0% | 0 |
| BTC hold | 25210 | 53.0% | +15210 |
| 50/50 BTC+ETH | 17730 | 59.9% | +7730 |

BTC made more money. It fails the 20% drawdown cap. Not eligible.

## D1 breakout, scale chosen on train

| Lookback | Train scale | Train end | Train DD | Holdout end | Holdout DD | 2x-fee holdout DD | Beat cash? |
|----------|-------------|-----------|----------|-------------|------------|-------------------|------------|
| 7 | 0.00 | 10000 | 0% | 10000 | 0% | 0% | no |
| 10 | 0.00 | 10000 | 0% | 10000 | 0% | 0% | no |
| 20 | 0.30 | 10262 | 16.3% | 10792 | 4.0% | 4.2% | yes |

Lookback 7 and 10: every positive size either broke the train drawdown cap or failed to beat cash. Optimum = cash.

Lookback 20 at 30% size: small train gain, holdout +7.9% with 4% drawdown. That is the only combo that cleared the bar.

## Looser cap (2026-11-12, user asked for a little more risk)

Same rule. Cap raised. Train still picks the scale. Holdout is not used to pick.

| Cap | Lookback 20 scale | Train end | Train DD | Holdout end | Holdout DD |
|-----|-------------------|-----------|----------|-------------|------------|
| 20% | 0.30 | 10262 | 16.3% | 10792 | 4.0% |
| 25% | 0.40 | 10305 | 21.2% | 11058 | 5.3% |
| 30% | 0.50 | 10326 | 25.8% | 11325 | 6.6% |
| 35% | 0.55 | 10328 | 28.0% | 11459 | 7.2% |

30% is the plateau. 35% adds $2 of train profit. Lookback 7 and 10 still pick cash at every cap.

SMA200 trend made $26,536 in train and $8,229 on holdout. Rejected. Train beauty, holdout loss.

## What the bot now uses

Unlocked `my_d1` only (locked D1 file not touched):

- lookback = 20
- exposure_scale = 0.50
- `config.yaml` paper kill switch `max_drawdown` = 0.30 (was 0.10)

## Verdict

Refine. Not Deploy.

- Holdout sample is about 1 year. Methodology wants 5+ years and 100+ trades before Deploy.
- Edge is small. 2x costs still fit under 20% DD at this size, but profit can vanish.
- Do not turn scale back up to chase the BTC hold number. That book drew down 53%.

Script: `scripts/risk_constrained_opt.py`
