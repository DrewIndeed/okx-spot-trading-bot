# Latest summary — real OKX data

Paper only. No live orders. Not a profit promise.

## 1. Book right now

Last closed daily bar: 2026-11-11. Live tickers pulled after that.

| Item | Value |
|------|--------|
| BTC live | 73370.6 |
| BTC last close | 74018.6 |
| BTC SMA200 | 73173 |
| Gate | ON |
| Buys now | none |
| Open breakouts | 0 |

The strategy does not buy because the price is ticking. It waits for the daily bar to close above the prior 20-day high, inside the top-20, with breadth and a stop under the close.

## 2. Closest names — live price, still not buys

| Coin | Live | Needs close above | Gap |
|------|------|-------------------|-----|
| AVAX | 8.631 | 9.126 | 5.7% |
| ICP | 2.563 | 2.739 | 6.9% |
| GRAM | 1.226 | 1.316 | 7.3% |
| XSPCX | 139.06 | 149.45 | 7.5% |
| LIT | 3.1719 | 3.4748 | 9.5% |
| BOME | 0.000587 | 0.000646 | 10.0% |
| BTC | 73370.6 | 81417 | 11.0% |
| ETHFI | 0.6145 | 0.684 | 11.3% |

Live price already above the old high, but the daily close has not confirmed:

| Coin | Live | Level | Above |
|------|------|-------|-------|
| NIGHT | 0.04683 | 0.04338 | 7.4% |
| KITE | 0.12381 | 0.12337 | 0.4% |

## 3. HYPE

| Item | Value |
|------|--------|
| Live | 71.15 |
| Last close | 72.08 |
| Breakout level | 89.98 |
| Status | NO ENTRY |
| Stop | none — 20-day mid 81.03 is above the price, so it is not a valid long stop |

## 4. What survived the risk test

Train picks the size. Holdout is the score. Cash is always allowed.

| Book | End | Max DD | Allowed? |
|------|-----|--------|----------|
| Cash | 10000 | 0% | yes |
| BTC hold | 25210 | 53% | no |
| 50/50 BTC+ETH | 17730 | 60% | no |
| D1 lookback 20, 30% size | holdout 10792 | 4.0% | yes |
| D1 lookback 20, 50% size | holdout 11325 | 6.6% | yes, this is what the bot uses |
| D1 lookback 20, 55% size | holdout 11459 | 7.2% | tiny extra gain, not used |
| SMA200 trend | train 26536, holdout 8229 | 37% train | no — pretty train, lost after |

Bot setting: `my_d1`, lookback 20, half size. Paper kill switch 30%.

## 5. How to read a good or bad result

| Word | Plain meaning |
|------|----------------|
| PF | Profit divided by loss. Under 1 means the losses were bigger. |
| maxDD | Deepest drop from a peak. |
| Pooled OOS | The part the rules did not get to peek at. Trust this more than the full window. |
| Gate ON | BTC close is above its 200-day average. New alts are allowed. |
| Flat | No coin passed the breakout rule. Cash is the position. |

## 6. Commands

```bash
python -m tradingbot signals --symbol HYPE --top 10
python -m tradingbot paper-once --strategy my_d1
python -m tradingbot backtest --no-update --strategies my_d1 --bar 1Dutc --days 800
```

Fresh board: `logs/signals_latest.md`
