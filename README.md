# makefun-btc-race-20261115

[![make.fun](https://make.fun/badge/btc-race-24h-20261115-0828.svg)](https://make.fun/bounty/btc-race-24h-20261115-0828)
[![rank](https://make.fun/badge/btc-race-24h-20261115-0828/dcm3llo/makefun-btc-race-20261115/rank.svg)](https://make.fun/bounty/btc-race-24h-20261115-0828/leaderboard)
[![reward](https://make.fun/badge/btc-race-24h-20261115-0828/dcm3llo/makefun-btc-race-20261115/reward.svg)](https://make.fun/bounty/btc-race-24h-20261115-0828)

Original deterministic, standard-library-only Python paper bot for make.fun's cbBTC race. No real trading, API keys, model calls or external data at runtime. Built locally; not submitted.

## Run

    python -P -s main.py < input.ndjson
    python -m unittest -v
    python backtest.py --kit /absolute/path/to/kit --out /absolute/path/outside/repo

Python 3.13 is the competition target. Local execution used Python 3.14.9; only portable stdlib features are used. The bot answers each batch with one compact JSON reply, echoes seq, emits nothing on hello/done, and flushes every reply. Input truncation, unexpected messages and sequence gaps fail closed.

## Strategy

Maintain 90 observed closes. Buy only when price is at least 150 bps below their arithmetic mean, using half current equity, limited by cash and the declared weight cap. The discount must also exceed four estimated round-trip costs, including depth-based impact. Sell the actual held quantity at a 75 bps mark-to-cost gain or 150 bps loss. The profit hurdle also rises to twice estimated transaction costs. A 100 bps slippage ceiling rejects large adverse gaps. Positions are reconciled from every supplied account, never from assumed fills.

Indicators and decisions use integers only. The rolling observation horizon is an ordinary strategy parameter, not a dev-window timetable. No timestamp, calendar date, seed, phase, total window length, future bar or dataset identity enters a decision. Missing bars do not advance history. No trading during an advertised warm-up.

## Published dev result

1,440 actual bars; compressed and decompressed SHA-256 verified against the published manifest. Exact integer next-open fills, 4 bps fees, 2 bps slippage, per-bar constant-product depth impact, max-slippage checks and final forced liquidation included.

| Strategy | Net USDC | Filled orders |
| --- | ---: | ---: |
| Entry | +10.857557 | 6 |
| Cash | 0 | 0 |
| Buy-and-hold | -44.009846 | 1 |
| 24-bar delayed hold | -41.070184 | 1 |
| EMA 8/21 control | -134.953038 | 63 |
| 60-bar breakout / 30-bar exit | -12.521741 | 13 |

Entry final cash: 1,010.857557 USDC. Fees: 1.208540 USDC. Maximum drawdown: 0.826977%. Doubled-fee adaptive replay: +9.642662 USDC (not the official fixed-action fee sensitivity statistic).

The entry subprocess ran twice over all actual dev batches with byte-identical orders, matching the in-process strategy exactly. Its orders were independently replayed through the unmodified published BarsEngine: every batch, fill and account matched, including the final cash; the engine reported no lookahead findings or abort.

## Research limits and blockers

The initial faster rebound/mean-normalization design lost 19.229740 USDC and was rejected. Coarse comparisons supported waiting for larger discounts and fixed risk/reward exits. The final lookback was chosen inside a profitable 60/90/120-bar neighborhood, not by maximum dev return. Nearby 175 bps entry remained profitable; reducing the entry band to 125 bps lost 2.494969 USDC. Increasing take-profit to 100 bps left only +0.053957 USDC. Selection used the public dev data; these are in-sample diagnostics, not independent validation. Only three completed round trips underlie the positive result. There is no claim of live profitability or prize eligibility.

The full kit grade-local Docker sandbox could not run because Docker's Linux daemon was unavailable; its process driver explicitly refuses Python. Direct use of the official BarsEngine verifies execution economics, not sandbox resource limits or all intake/transcript gates. Native transcript check results and remaining blockers are recorded in the local READY.json (excluded from this repository). No submission was sent and no competition quota was consumed.

Kit, dataset, harness dependencies, transcript bundle, research scratch and execution logs are deliberately outside the committed tree. backtest.py is a single-asset/single-order research evaluator; it is not a complete multi-order harness implementation. The EMA and breakout controls are sensible comparison strategies, not asserted to be byte-identical copies of house bots.

License: MIT.
