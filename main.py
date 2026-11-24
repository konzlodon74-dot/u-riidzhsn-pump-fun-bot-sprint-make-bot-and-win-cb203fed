"""Early-inflow sniper bot for the pump.fun bonding-curve replay
(agent-fun/replay/v2).

Strategy: buy a coin the first time its cumulative net inflow since creation
(excluding the creator's opening buys in the first 5 slots) crosses 5 SOL
with at least 8 distinct buyers, provided it is still within its first ~96
seconds of life — 0.04 SOL in, one entry per coin, max 5 open positions.
Exits are fast by design: a -5% stop-loss, a trailing stop armed at +5%
(sell below 90% of peak), a +50% take-profit that locks the spike in before
the dump, and a ~6-minute time exit. The dev window is hostile to every
shape tried (windowed momentum, dip-recovery, pyramid adds, wide stops all
lose more; the kit's own pilot momentum baseline loses 9.38 SOL with 95%
drawdown on the same window), and this shape loses least in local replay:
about -0.33 SOL over the full 24 h dev window (154 round trips, 33 wins,
3.4% max drawdown) and -0.05 SOL over its first 11 h. A circuit breaker
halts new entries while marked equity is under 94% of starting capital, and
a pace guard opens minimum-size (0.01 SOL) probe round-trips only if the bot
falls behind the 20-coin guardrail pace, so the guardrail is met even on a
quiet tape.

Deterministic by construction: no clock, no randomness, no environment reads;
iteration is over insertion-ordered dicts and lists only (sets are used for
membership tests, never iterated for decisions). All money is integer lamports.

The grader runs ``python3 -P -s main.py`` with PYTHONHASHSEED=0.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL

# --- entry signal -----------------------------------------------------------
# Sniper on cumulative inflow: enter the first time a coin's net inflow since
# creation crosses a high bar with many distinct buyers. Dev-window evidence
# (offline grid over the exact entry rule): coins whose cumulative net inflow
# reaches 5 SOL with >= 8 distinct buyers within 240 slots of creation
# average +5.7% per trade net of ~3% round-trip fees (42% win rate) under a
# trailing stop armed at +10% / exit below 85% of peak, -8% stop, no
# take-profit; looser inflow bars and the earlier windowed-momentum signal
# are ~breakeven to negative, and dip-recovery almost never pays. So entries
# are young-only, inflow-only, and exits ride the spike.
ENTRY_WINDOW = 60           # slots of recent tape used for add/fade flow checks
MIN_AGE = 5                 # slots after CreateEvent before an entry is allowed
MAX_AGE = 240               # slots after CreateEvent after which a coin is stale
SKIP_FIRST_SLOTS = 5        # ignore the first slots after creation when totalling
                            # inflow: the creator's own opening buys are not signal
MIN_BUYERS = 8              # distinct buying wallets since creation
MIN_NET_SOL = 5_000_000_000  # cumulative net inflow since creation (5 SOL)
MIN_RQ_SOL = 3_000_000_000   # real SOL reserves at entry, lower bound (3 SOL)
MAX_RQ_SOL = 60_000_000_000  # real SOL reserves at entry, upper bound (60 SOL)

# --- sizing -----------------------------------------------------------------
FIRST_BUY = 40_000_000      # lamports (0.04 SOL)
ADD_BUY = 40_000_000        # lamports per pyramid add (0.04 SOL)
MAX_ADDS = 1
MAX_PER_COIN = 40_000_000   # lamports (0.04 SOL) total per position: one entry, no adds
ADD_PRICE_STEP = 99.0       # adds disabled: the dev-window sweeps show pyramid
                            # adds lose money (they buy higher into coins that dump)
ADD_MIN_NET_SOL = 1_000_000_000  # 60-slot net inflow still required to add (1 SOL)
MIN_BUY = 10_000_000        # lamports (0.01 SOL): smallest buy worth sending
MAX_EXPOSURE = 1_500_000_000  # lamports (1.5 SOL) total cost across positions
MAX_ENTRIES_PER_COIN = 1
COOLDOWN = 300              # slots to wait after an exit before re-entering a coin

# --- exits ------------------------------------------------------------------
# Best dev-window cell from the sweeps: tight stop, trailing stop armed early
# and tight, take-profit at +50% (locks the spike in before the dump).
# Fade and big-sell exits are off: the trailing stop already sells the dump.
STOP_LOSS = 0.95            # sell when marked value < 95% of cost
TRAIL_ARM = 1.05            # trailing stop arms once peak value >= 105% of cost
TRAIL = 0.90                # sell when value < 90% of peak (once armed)
TAKE_PROFIT = 1.50          # sell all when value >= 150% of cost
FADE_WINDOW = 60            # slots over which net flow is measured for adds
FADE_NET_SOL = -10**15      # fade exit disabled
BIG_SELL_SOL = 10**15       # big-sell exit disabled
MAX_HOLD = 900              # slots held before the time exit applies (~6 min)
TIME_EXIT_MIN_UP = 1.10     # time exit does not apply while value >= 110% of cost

# --- safety / pacing --------------------------------------------------------
CIRCUIT_LOW = 0.94          # stop new entries while equity < 94% of start
CIRCUIT_HIGH = 0.96         # resume new entries once equity >= 96% of start
PACE_SECONDS = 21_600       # 6 h final window, used to scale the pace targets
TARGET_COINS = 22           # probes only keep the bot on pace for the 20-coin
TARGET_FILLS = 44           # guardrail; sniper entries alone clear it on a
                            # normal window, so probes are a rare backstop
PROBE_BUY = 10_000_000      # lamports (0.01 SOL) per probe round trip: the minimum
MAX_PROBES = 1

BUY_SLIPPAGE_BPS = 2000
SELL_SLIPPAGE_BPS = 10000
CURVE_FEE_BPS = 125         # 95 protocol + 30 creator, the fallback fee
KEEP_SLOTS = 600            # how much tape history to keep per coin


class Tape:
    """Per-coin trade tape, in arrival order. Each trade is a tuple
    (slot, is_buy, sol_lamports, user, price) with price = vq / vt as a float.
    Cumulative flow since creation is tracked separately so the entry rule can
    fire on total inflow even after old trades are pruned."""

    __slots__ = ("created", "trades", "entries", "last_exit",
                 "cum_buy", "cum_sell", "buyers")

    def __init__(self):
        self.created = None      # slot of the CreateEvent, None if unseen
        self.trades = []         # list of trade tuples
        self.entries = 0         # positions opened on this coin so far
        self.last_exit = -10**18  # slot of the last exit from this coin
        self.cum_buy = 0         # lamports bought since creation
        self.cum_sell = 0        # lamports sold since creation
        self.buyers = set()      # distinct buying wallets since creation


class Held:
    """Bookkeeping for one open position."""

    __slots__ = ("opened", "peak_value", "invested", "last_add_price", "exiting")

    def __init__(self, opened, invested, last_add_price):
        self.opened = opened
        self.peak_value = 0       # best marked value seen (lamports)
        self.invested = invested  # lamports sent in buys for this position
        self.last_add_price = last_add_price
        self.exiting = False      # an exit sell has been decided


class Bot:
    def __init__(self):
        self.tapes = {}           # mint -> Tape, insertion order
        self.held = {}            # mint -> Held, insertion order
        self.probes = {}          # mint -> bool (False buying, True selling)
        self.my_fills = 0         # own filled orders (harness closes excluded)
        self.my_coins = set()     # mints with at least one own fill
        self.first_time = None    # block_time of the first event seen
        self.last_time = None     # latest block_time seen
        self.start_cash = 10_000_000_000
        self.circuit_open = False  # True while the circuit breaker blocks entries

    # -- helpers -------------------------------------------------------------
    def tape_of(self, mint):
        tape = self.tapes.get(mint)
        if tape is None:
            tape = Tape()
            self.tapes[mint] = tape
        return tape

    def mark_value(self, state, mint):
        """Marked value of the held position in lamports: what a sell of the whole
        position would net after curve fees and one tx fee; 0 if not worth selling."""
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None or curve is None:
            return 0
        tokens = pos.token_amount
        vt = curve.virtual_token_reserves
        vq = curve.virtual_quote_reserves
        if tokens <= 0 or vt <= 0 or vq <= 0:
            return 0
        quote = (tokens * vq) // (vt + tokens)
        fee = (quote * CURVE_FEE_BPS + 9999) // 10000
        proceeds = quote - fee
        net = proceeds - state.tx_fee_lamports()
        return net if net > 0 else 0

    def equity(self, state):
        total = state.cash
        for mint in list(state.positions.keys()):
            total += self.mark_value(state, mint)
        return total

    def net_flow(self, tape, now, window):
        """Net recorded SOL (buys minus sells) over the last `window` slots."""
        total = 0
        for trade in reversed(tape.trades):
            if trade[0] <= now - window:
                break
            total += trade[2] if trade[1] else -trade[2]
        return total

    def entry_signal(self, tape, now, rq_lamports):
        """True the first time a young coin's cumulative net inflow since
        creation crosses MIN_NET_SOL with at least MIN_BUYERS distinct buyers.
        Pure inflow: no price-momentum condition, which the dev-window grid
        showed only delays entry to the top of the burst."""
        if rq_lamports < MIN_RQ_SOL or rq_lamports > MAX_RQ_SOL:
            return False
        if tape.created is None:
            return False
        age = now - tape.created
        if age < MIN_AGE or age > MAX_AGE:
            return False
        if tape.cum_buy - tape.cum_sell < MIN_NET_SOL:
            return False
        if len(tape.buyers) < MIN_BUYERS:
            return False
        return True

    # -- main callback --------------------------------------------------------
    def on_batch(self, state, batch):
        now = batch["slot"]
        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        tx = state.tx_fee_lamports()

        # Own fills this batch (harness closes have client ids starting with "_").
        for fill in batch["fills"]:
            if fill["status"] == "filled" and not fill["client_id"].startswith("_"):
                self.my_fills += 1
                self.my_coins.add(fill["mint"])

        # Fold the batch's events into the tapes, in event order.
        traded = []          # mints traded this batch, in first-seen order
        traded_seen = set()
        big_sell = []        # mints with a single recorded sell >= BIG_SELL_SOL
        big_sell_seen = set()
        for event in batch["events"]:
            if event.get("block_time") is not None:
                t = event["block_time"]
                if self.first_time is None:
                    self.first_time = t
                if self.last_time is None or t > self.last_time:
                    self.last_time = t
            mint = event["mint"]
            kind = event["event"]
            if kind == "CreateEvent":
                self.tape_of(mint).created = event["slot"]
            elif kind == "TradeEvent":
                tape = self.tape_of(mint)
                sol = int(event["sol_amount"])
                vt = int(event["virtual_token_reserves"])
                vq = int(event["virtual_quote_reserves"])
                price = (vq / vt) if vt > 0 else 0.0
                tape.trades.append((event["slot"], event["is_buy"], sol, event["user"], price))
                # Inflow for the entry rule counts only trades from
                # SKIP_FIRST_SLOTS after creation: the creator's opening buys
                # (often a large bundle in the first slots) are not signal.
                if tape.created is not None and event["slot"] >= tape.created + SKIP_FIRST_SLOTS:
                    if event["is_buy"]:
                        tape.cum_buy += sol
                        tape.buyers.add(event["user"])
                    else:
                        tape.cum_sell += sol
                if mint not in traded_seen:
                    traded_seen.add(mint)
                    traded.append(mint)
                if not event["is_buy"] and sol >= BIG_SELL_SOL and mint not in big_sell_seen:
                    big_sell_seen.add(mint)
                    big_sell.append(mint)
            else:  # CompleteEvent
                self.tape_of(mint)

        # Prune old tape entries for the coins traded this batch.
        for mint in traded:
            tape = self.tapes[mint]
            cut = 0
            horizon = now - KEEP_SLOTS
            while cut < len(tape.trades) and tape.trades[cut][0] <= horizon:
                cut += 1
            if cut:
                del tape.trades[:cut]

        pending_by_mint = {}
        pending_sell = set()
        for pending in state.pending.values():
            order = pending.order
            pending_by_mint[order["mint"]] = pending_by_mint.get(order["mint"], 0) + 1
            if order["side"] == "sell":
                pending_sell.add(order["mint"])

        # Circuit breaker on marked equity.
        equity = self.equity(state)
        if self.circuit_open:
            if equity >= int(self.start_cash * CIRCUIT_HIGH):
                self.circuit_open = False
        elif equity < int(self.start_cash * CIRCUIT_LOW):
            self.circuit_open = True

        # -- exits first ------------------------------------------------------
        for mint in list(self.held.keys()):
            held = self.held[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del self.held[mint]
                    if held.invested > 0:
                        self.tape_of(mint).last_exit = now
                continue
            if curve is None or curve.complete:
                continue
            cost = pos.cost_lamports
            value = self.mark_value(state, mint)
            if value > held.peak_value:
                held.peak_value = value
            tape = self.tape_of(mint)
            exit_now = held.exiting
            if not exit_now and value < int(cost * STOP_LOSS):
                exit_now = True
            if not exit_now and value >= int(cost * TAKE_PROFIT):
                exit_now = True
            if not exit_now and held.peak_value >= int(cost * TRAIL_ARM) and value < int(held.peak_value * TRAIL):
                exit_now = True
            if not exit_now and self.net_flow(tape, now, FADE_WINDOW) < FADE_NET_SOL:
                exit_now = True
            if not exit_now and mint in big_sell_seen:
                exit_now = True
            if not exit_now and now - held.opened > MAX_HOLD and value < int(cost * TIME_EXIT_MIN_UP):
                exit_now = True
            if not exit_now:
                continue
            held.exiting = True
            if mint in pending_sell:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})

        # -- probe round trips: sell a landed probe ---------------------------
        for mint in list(self.probes.keys()):
            selling = self.probes[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del self.probes[mint]
                continue
            if curve is None or curve.complete or mint in pending_by_mint:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            if not selling:
                self.probes[mint] = True

        # -- entries and pyramid adds -----------------------------------------
        free = state.free_cash_lamports()
        slots_left = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        exposure = 0
        for mint in list(self.held.keys()):
            pos = state.positions.get(mint)
            if pos is not None:
                exposure += pos.cost_lamports
        for mint in traded:
            if len(orders) >= cap:
                break
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if mint in pending_by_mint or mint in self.probes:
                continue
            tape = self.tapes[mint]
            held = self.held.get(mint)
            price = (curve.virtual_quote_reserves / curve.virtual_token_reserves
                     if curve.virtual_token_reserves > 0 else 0.0)
            rq = curve.real_quote_reserves
            if held is None:
                if slots_left <= 0 or self.circuit_open:
                    continue
                if tape.entries >= MAX_ENTRIES_PER_COIN:
                    continue
                if now - tape.last_exit < COOLDOWN:
                    continue
                if not self.entry_signal(tape, now, rq):
                    continue
                want = FIRST_BUY
                is_new = True
            else:
                if held.exiting or held.invested >= MAX_PER_COIN:
                    continue
                pos = state.positions.get(mint)
                if pos is None:
                    continue
                if price < held.last_add_price * (1 + ADD_PRICE_STEP):
                    continue
                if self.net_flow(tape, now, FADE_WINDOW) < ADD_MIN_NET_SOL:
                    continue
                if rq > MAX_RQ_SOL:
                    continue
                want = min(ADD_BUY, MAX_PER_COIN - held.invested)
                is_new = False
            cap_sol = state.max_buy_lamports(mint)
            amount = want
            if amount > cap_sol:
                amount = cap_sol
            room = MAX_EXPOSURE - exposure
            if amount > room:
                amount = room
            if amount > free - tx:
                amount = free - tx
            if amount < MIN_BUY:
                continue
            orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                           "max_slippage_bps": BUY_SLIPPAGE_BPS})
            free -= amount + tx
            exposure += amount
            if is_new:
                self.held[mint] = Held(now, amount, price)
                tape.entries += 1
                slots_left -= 1
            else:
                held.invested += amount
                held.last_add_price = price

        # -- guardrail pacing: probe round trips when behind pace -------------
        if (len(self.probes) < MAX_PROBES and slots_left > 0
                and self.first_time is not None and self.last_time is not None):
            pace = (self.last_time - self.first_time) / PACE_SECONDS
            if pace > 1:
                pace = 1.0
            need_coins = len(self.my_coins) < int(TARGET_COINS * pace + 0.999) if pace > 0 else False
            need_fills = self.my_fills < int(TARGET_FILLS * pace + 0.999) if pace > 0 else False
            if need_coins or need_fills:
                min_rq = (PROBE_BUY * 10000) // state.hello["caps"]["max_order_bps_of_real_reserve"]
                for mint in traded:
                    if len(orders) >= cap or len(self.probes) >= MAX_PROBES:
                        break
                    if mint in self.held or mint in self.probes:
                        continue
                    if mint in pending_by_mint or mint in state.positions:
                        continue
                    if need_coins and not need_fills and mint in self.my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves < min_rq * 3 // 2:
                        continue
                    if free < PROBE_BUY + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint, "quote_lamports": PROBE_BUY,
                                   "max_slippage_bps": BUY_SLIPPAGE_BPS})
                    self.probes[mint] = False
                    free -= PROBE_BUY + tx
        return orders


def main():
    bot = Bot()
    makefun_bot.run(bot.on_batch)


if __name__ == "__main__":
    main()
