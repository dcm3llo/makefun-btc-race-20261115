"""Causal, cost-aware cbBTC paper trader; Python standard library only."""
import collections
import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"


class Strategy:
    """Buy a large rolling-mean discount; exit at a cost-aware target or stop.

    All indicators use closes already received. No seed, timestamp, phase,
    dataset identifier or terminal horizon enters a trading decision.
    """

    def __init__(self, hello, lookback=90, entry_bps=150, take_bps=75, risk_bps=150):
        self.hello = hello
        self.spec = hello["symbols"][0]
        self.symbol = self.spec["symbol"]
        self.closes = collections.deque(maxlen=lookback)
        self.lookback = lookback
        self.entry_bps = entry_bps
        self.take_bps = take_bps
        self.risk_bps = risk_bps
        self.counter = 0

    def value(self, qty, price):
        return (qty * price * 10 ** self.hello["capital"]["quote_scale"]
                // 10 ** (self.spec["qty_scale"] + self.spec["price_scale"]))

    def on_batch(self, batch):
        bars = [b for b in batch["bars"] if b["symbol"] == self.symbol]
        if not bars:
            return []
        bar = bars[0]
        close = int(bar["c"])
        self.closes.append(close)
        account = batch["account"]
        position = next((p for p in account["positions"]
                         if p["symbol"] == self.symbol), None)
        qty = int(position["qty"]) if position else 0
        if len(self.closes) < self.lookback:
            return []
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        # Half-equity exposure limits both adverse selection and impact.
        amount = min(cash, equity // 2)
        depth = [int(d) for d in bar["c_depth"]]
        fee = self.hello["fees"]["fee_bps"]
        slip = self.hello["fees"]["slippage_bps"]
        # Round up each hop's impact; prices and decisions remain integer-only.
        impact_bps = sum((amount * 10000 + d - 1) // d for d in depth)
        round_trip = 2 * (fee + slip + impact_bps)
        entry_band = max(self.entry_bps, 4 * round_trip)
        profit_band = max(self.take_bps, 2 * round_trip)
        side = None
        if position is not None and qty:
            cost = int(position["cost_quote_units"])
            value = self.value(qty, close)
            if (value * 10000 >= cost * (10000 + profit_band)
                    or value * 10000 <= cost * (10000 - self.risk_bps)):
                side = "sell"
        elif close * len(self.closes) * 10000 < sum(self.closes) * (10000 - entry_band):
            side = "buy"
        if batch["seq"] < self.hello.get("warmup_bars", 0):
            return []
        if side is None:
            return []
        minimum = int(self.hello["fees"]["min_notional_quote_units"])
        room = equity * self.hello["caps"]["max_weight_bps"] // 10000
        amount = min(amount, room)
        if side == "buy" and (amount < minimum or self.hello["caps"]["max_open_positions"] < 1):
            return []
        self.counter += 1
        order = {"side": side, "client_id": "r" + str(self.counter),
                 "symbol": self.symbol, "max_slippage_bps": 100}
        order["qty" if side == "sell" else "notional_quote_units"] = str(qty if side == "sell" else amount)
        return [order]


def run(source=None, output=None):
    source = sys.stdin.buffer if source is None else source
    output = sys.stdout if output is None else output
    strategy = None
    seq = -1
    while True:
        raw = source.readline(1048578)
        if not raw:
            raise ValueError("input ended before done")
        if len(raw.rstrip(b"\r\n")) > 1048576:
            raise ValueError("harness line too large")
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "hello":
            if strategy is not None or message.get("protocol") != PROTOCOL:
                raise ValueError("unexpected hello or protocol")
            strategy = Strategy(message)
        elif kind in ("batch", "done"):
            if strategy is None or type(message.get("seq")) is not int or message["seq"] != seq + 1:
                raise ValueError("message sequence violation")
            if kind == "done":
                return
            seq = message["seq"]
            orders = strategy.on_batch(message)
            reply = {"type": "reply", "seq": seq, "orders": orders}
            output.write(json.dumps(reply, separators=(",", ":"), allow_nan=False) + "\n")
            output.flush()
        else:
            raise ValueError("unknown message type")


if __name__ == "__main__":
    # Fix wire newlines on Windows too; replay logs are byte-comparable.
    sys.stdout.reconfigure(newline="\n")
    run()
