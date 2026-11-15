"""Local one-asset, one-order replay using the published integer fill equations.

Data and generated logs must be supplied outside this repository. This is not
an exchange connector. Official kit grading remains the final authority.
"""
import argparse
import copy
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

# The grader's -P flag affects main.py only; this research tool is run normally.
from main import PROTOCOL, Strategy


def load_data(kit):
    root = Path(kit) / "dataset"
    manifest = json.loads((root / "manifest.json").read_text())
    spec = manifest["symbols"][0]
    raw = (root / spec["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == spec["sha256"]
    decoded = gzip.decompress(raw)
    assert hashlib.sha256(decoded).hexdigest() == spec["ndjson_sha256"]
    bars = [json.loads(line) for line in decoded.splitlines()]
    assert len(bars) == spec["bars"] == manifest["window"]["bars"]
    hello = {"type": "hello", "protocol": PROTOCOL, "seed": "0",
             "symbols": [{k: spec[k] for k in ("symbol", "mint", "price_scale", "qty_scale")}],
             "capital": {"quote": "USDC", "quote_scale": 6, "quote_units": "1000000000"},
             "bar_interval": "1m", "first_close_ms": bars[0]["t"],
             "caps": {"max_open_positions": 1, "max_orders_per_bar": 16,
                      "max_weight_bps": 10000, "shorts": False, "leverage": False},
             "fees": {"fee_bps": 4, "slippage_bps": 2, "min_notional_quote_units": "1000000"},
             "latency_bars": 1}
    return hello, bars, manifest


class Replay:
    def __init__(self, hello, fee_multiplier=1):
        self.hello = hello
        self.spec = hello["symbols"][0]
        self.cash = int(hello["capital"]["quote_units"])
        self.start = self.cash
        self.qty = 0
        self.cost = 0
        self.fee = hello["fees"]["fee_bps"] * fee_multiplier
        self.slip = hello["fees"]["slippage_bps"]
        self.fees = 0
        self.trades = 0
        self.rejects = []
        self.resolutions = []
        self.marks = [self.cash]

    def value(self, qty, price):
        return qty * price * 10 ** self.hello["capital"]["quote_scale"] // 10 ** (self.spec["qty_scale"] + self.spec["price_scale"])

    def account(self, price):
        pos = [] if not self.qty else [{"symbol": self.spec["symbol"], "qty": str(self.qty), "cost_quote_units": str(self.cost)}]
        return {"cash_quote_units": str(self.cash), "positions": pos,
                "equity_quote_units": str(self.cash + self.value(self.qty, price))}

    def execute(self, order, bar, reference, seq, flatten=False):
        buy = order["side"] == "buy"
        price = int(bar["c" if flatten else "o"])
        depths = [int(d) for d in bar["c_depth" if flatten else "o_depth"]]
        amount = int(order["notional_quote_units"] if buy else order["qty"])
        reason = None
        minimum = int(self.hello["fees"]["min_notional_quote_units"])
        if amount <= 0:
            reason = "zero_amount"
        elif buy and amount > self.cash:
            reason = "insufficient_cash"
        elif not buy and amount > self.qty:
            reason = "insufficient_position"
        elif buy and amount < minimum:
            reason = "below_min_notional"
        elif not buy and amount != self.qty and self.value(amount, reference) < minimum:
            reason = "below_min_notional"
        spend = amount * (10000 - self.fee) // 10000 if buy else self.value(amount, price)
        # Fresh pool on every open; only one order per bar is supported here.
        quote_scale = self.hello["capital"]["quote_scale"]
        factor = 1 + sum((Fraction(spend * 10 ** 6, d * 10 ** quote_scale) for d in depths), Fraction())
        if buy:
            executed = price * (10000 + self.slip) * factor.numerator // (10000 * factor.denominator)
        else:
            executed = max(1, price * (10000 - self.slip) * factor.denominator // (10000 * factor.numerator))
        if not flatten:
            limit = order["max_slippage_bps"]
            if buy and executed * 10000 > reference * (10000 + limit):
                reason = reason or "max_slippage"
            if not buy and executed * 10000 < reference * (10000 - limit):
                reason = reason or "max_slippage"
        result = {"client_id": order["client_id"], "symbol": self.spec["symbol"], "side": order["side"],
                  "status": "rejected" if reason else "filled", "reason": reason,
                  "decided_bar": bar["t"] if flatten else bar["t"] - 60000,
                  "fill_bar": bar["t"], "price": "0", "qty": "0",
                  "notional_quote_units": "0", "fee_quote_units": "0"}
        if reason:
            self.rejects.append(reason)
            return result
        if buy:
            quantity = spend * 10 ** (self.spec["qty_scale"] + self.spec["price_scale"]) // (executed * 10 ** quote_scale)
            paid_fee = amount - spend
            self.cash -= amount
            self.qty += quantity
            self.cost += amount
            notional = amount
        else:
            quantity = amount
            gross = self.value(quantity, executed)
            proceeds = gross * (10000 - self.fee) // 10000
            paid_fee = gross - proceeds
            self.cash += proceeds
            self.cost -= self.cost * quantity // self.qty
            self.qty -= quantity
            notional = proceeds
        self.fees += paid_fee
        if not flatten:
            self.trades += 1
        result.update(price=str(executed), qty=str(quantity), notional_quote_units=str(notional), fee_quote_units=str(paid_fee))
        self.resolutions.append(result)
        return result


def replay(hello, bars, callback, fee_multiplier=1):
    book = Replay(hello, fee_multiplier)
    pending = []
    reference = int(bars[0]["c"])
    replies = []
    messages = [hello]
    for seq, raw in enumerate(bars):
        bar = {k: raw[k] for k in ("t", "o", "h", "l", "c", "v", "o_depth", "c_depth")}
        bar["symbol"] = hello["symbols"][0]["symbol"]
        fills = [book.execute(order, bar, reference, seq) for order in pending]
        reference = int(bar["c"])
        batch = {"type": "batch", "seq": seq, "t": bar["t"], "bars": [bar], "fills": fills, "account": book.account(reference)}
        book.marks.append(int(batch["account"]["equity_quote_units"]))
        messages.append(copy.deepcopy(batch))
        orders = callback(batch)
        assert len(orders) <= 1, "this evaluator only supports a fresh pool's single order"
        pending = copy.deepcopy(orders)
        replies.append({"type": "reply", "seq": seq, "orders": orders})
    book.rejects.extend("after_end" for _ in pending)
    if book.qty:
        book.execute({"side": "sell", "qty": str(book.qty), "client_id": "_flatten_0"}, bars[-1], reference, len(bars), True)
    book.marks.append(book.cash)
    messages.append({"type": "done", "seq": len(bars), "reason": "end_of_data", "account": book.account(reference)})
    peak = book.start
    drawdown = 0
    for equity in book.marks:
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    encoded = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in replies).encode()
    result = {"net_pnl_quote_units": book.cash - book.start,
              "net_pnl_usdc": (book.cash - book.start) / 1e6,
              "final_cash_quote_units": book.cash, "fees_quote_units": book.fees,
              "trades": book.trades, "max_drawdown": drawdown, "rejections": book.rejects,
              "action_sha256": hashlib.sha256(encoded).hexdigest()}
    return result, messages, encoded, book


class Comparison:
    """Simple controls, intentionally not imported into the trading entrypoint."""
    def __init__(self, hello, mode):
        self.hello, self.mode = hello, mode
        self.history = []
        self.fast = self.slow = None
        self.count = 0

    def on_batch(self, batch):
        c = int(batch["bars"][0]["c"])
        self.history.append(c)
        self.fast = c if self.fast is None else self.fast + 2 * (c - self.fast) / 9
        self.slow = c if self.slow is None else self.slow + 2 * (c - self.slow) / 22
        held = batch["account"]["positions"]
        side = None
        if self.mode == "cash":
            return []
        if self.mode in ("hold", "momentum24"):
            if not held and len(self.history) == (1 if self.mode == "hold" else 24):
                side = "buy"
        elif self.mode == "ema8-21" and len(self.history) >= 21:
            if not held and self.fast > self.slow:
                side = "buy"
            elif held and self.fast < self.slow:
                side = "sell"
        elif self.mode == "breakout60" and len(self.history) > 60:
            if not held and c > max(self.history[-61:-1]):
                side = "buy"
            elif held and c < min(self.history[-31:-1]):
                side = "sell"
        if side is None:
            return []
        self.count += 1
        order = {"side": side, "client_id": "c" + str(self.count), "symbol": self.hello["symbols"][0]["symbol"], "max_slippage_bps": 100}
        if side == "buy":
            order["notional_quote_units"] = batch["account"]["cash_quote_units"]
        else:
            order["qty"] = held[0]["qty"]
        return [order]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True)
    parser.add_argument("--out", required=True, help="output directory, outside repository")
    args = parser.parse_args()
    hello, bars, manifest = load_data(args.kit)
    output = Path(args.out).resolve()
    repo = Path(__file__).resolve().parent
    if output == repo or repo in output.parents:
        parser.error("keep data and logs outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    for mode in ("cash", "hold", "momentum24", "ema8-21", "breakout60"):
        results[mode] = replay(hello, bars, Comparison(hello, mode).on_batch)[0]
    strategy = Strategy(hello)
    result, messages, expected, book = replay(hello, bars, strategy.on_batch)
    results["entry"] = result
    results["entry_2x_fees_adaptive"] = replay(hello, bars, Strategy(hello).on_batch, 2)[0]
    results["neighborhood"] = {}
    for options in ({"lookback": 60}, {"lookback": 120}, {"entry_bps": 125},
                    {"entry_bps": 175}, {"take_bps": 100}, {"risk_bps": 200}):
        key = json.dumps(options, sort_keys=True)
        results["neighborhood"][key] = replay(hello, bars, Strategy(hello, **options).on_batch)[0]
    wire = b"".join((json.dumps(m, separators=(",", ":")) + "\n").encode() for m in messages)
    runs = [subprocess.run([sys.executable, "-P", "-s", str(repo / "main.py")], input=wire, capture_output=True, check=True).stdout for _ in range(2)]
    assert runs[0] == runs[1] == expected, "subprocess protocol output or determinism mismatch"
    results["protocol"] = {"batch_replies": len(bars), "executions": 2, "byte_identical": True, "matches_strategy": True}
    results["dataset_sha256"] = manifest["dataset_sha256"]
    results["bars"] = len(bars)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (output / "input.ndjson").write_bytes(wire)
    (output / "orders.ndjson").write_bytes(expected)
    (output / "fills.json").write_text(json.dumps(book.resolutions, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
