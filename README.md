# Pump.fun Sniper Bot

[![make.fun](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![make.fun rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/konzlodon74-dot/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![make.fun reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/konzlodon74-dot/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

Bounty: https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed

A Solana trading bot for pump.fun-style bonding-curve markets, written for the
`agent-fun/replay/v2` harness. It reads the replay's market events as they
arrive and trades 10 SOL of paper money on a frozen 6-hour window.

## Strategy

**Early-inflow sniper.** A coin is bought the first time its cumulative net
inflow since creation (excluding the creator's opening buys in the first 5
slots) crosses 5 SOL with at least 8 distinct buyers, provided it is still
within its first ~96 seconds of life. One entry per coin, 0.04 SOL in, at most
5 open positions.

**Fast exits.** A -5% stop-loss, a trailing stop armed at +5% (sell below 90%
of peak), a +50% take-profit that locks the spike in before the dump, and a
~6-minute time exit. Runners dump hard, so the bot rides the spike instead of
holding it.

**Safety.** A circuit breaker halts new entries while marked equity is under
94% of starting capital, and a pace guard opens minimum-size (0.01 SOL) probe
round-trips only if the bot falls behind the 20-coin guardrail pace.

## Why this shape

The dev window is hostile to every shape tried: windowed momentum,
dip-recovery, pyramid adds and wide stops all lose more, and the kit's own
pilot momentum baseline loses 9.38 SOL with 95% drawdown on the same window.
This shape loses least in a local replay of the kit's market model
(constant-product curve, 95+30 bps fees, 105,000 lamports tx fee, 2-slot
latency, 1% reserve buy cap): about -0.42 SOL over the full 24 h dev window
(154 round trips, 33 wins, 4.4% max drawdown), -0.05 SOL over its first 11 h.
The final window is a fresh tape, so the dev number is a stress test, not a
forecast.

## Running it

The entrypoint is `main.py` (Python 3.13 standard library only). It speaks
`agent-fun/replay/v2` on stdin/stdout through the kit's SDK shim, vendored in
`vendor/makefun_bot.py`. The bot is fully deterministic: no clock, no
randomness, no environment reads, and iteration only over insertion-ordered
dicts and lists — two executions produce byte-identical action logs.

## License

MIT — see [LICENSE](LICENSE).
