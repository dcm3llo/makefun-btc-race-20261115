"""Synthetic unit tests; dev-data execution lives in backtest.py."""
import copy
import io
import json
import unittest
from main import PROTOCOL, Strategy, run
from backtest import Replay, replay


def hello():
    return {"type": "hello", "protocol": PROTOCOL, "seed": "0",
            "symbols": [{"symbol": "CBBTC", "mint": "test", "price_scale": 5, "qty_scale": 8}],
            "capital": {"quote": "USDC", "quote_scale": 6, "quote_units": "1000000000"},
            "caps": {"max_open_positions": 1, "max_orders_per_bar": 16, "max_weight_bps": 10000},
            "fees": {"fee_bps": 4, "slippage_bps": 2, "min_notional_quote_units": "1000000"}}


def bar(price=10000000000, t=60000):
    return {"symbol": "CBBTC", "t": t, "o": str(price), "h": str(price), "l": str(price),
            "c": str(price), "v": None, "o_depth": ["1000000000000"], "c_depth": ["1000000000000"]}


def batch(seq=0, price=10000000000):
    return {"type": "batch", "seq": seq, "t": 60000 * (seq + 1), "bars": [bar(price)], "fills": [],
            "account": {"cash_quote_units": "1000000000", "positions": [], "equity_quote_units": "1000000000"}}


def wire(messages):
    return io.BytesIO(b"".join((json.dumps(m) + "\n").encode() for m in messages))


class ProtocolTests(unittest.TestCase):
    def test_one_reply_per_batch_and_none_for_hello_done(self):
        messages = [hello(), batch(), {"type": "done", "seq": 1}]
        out = io.StringIO()
        run(wire(messages), out)
        self.assertEqual(json.loads(out.getvalue()), {"type": "reply", "seq": 0, "orders": []})

    def test_unknown_message(self):
        with self.assertRaises(ValueError):
            run(wire([hello(), {"type": "surprise"}]), io.StringIO())

    def test_batch_before_hello(self):
        with self.assertRaises(ValueError):
            run(wire([batch()]), io.StringIO())

    def test_duplicate_hello(self):
        with self.assertRaises(ValueError):
            run(wire([hello(), hello()]), io.StringIO())

    def test_wrong_protocol(self):
        h = hello()
        h["protocol"] = "other"
        with self.assertRaises(ValueError):
            run(wire([h]), io.StringIO())

    def test_sequence_gap(self):
        with self.assertRaises(ValueError):
            run(wire([hello(), batch(1)]), io.StringIO())

    def test_eof_is_not_done(self):
        with self.assertRaises(ValueError):
            run(wire([hello(), batch()]), io.StringIO())

    def test_line_size(self):
        with self.assertRaises(ValueError):
            run(io.BytesIO(b" " * 1048577 + b"\n"), io.StringIO())

    def test_deterministic_wire(self):
        messages = [hello()] + [batch(i) for i in range(100)] + [{"type": "done", "seq": 100}]
        outputs = []
        for _ in range(2):
            out = io.StringIO()
            run(wire(messages), out)
            outputs.append(out.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(outputs[0].splitlines()), 100)


class StrategyTests(unittest.TestCase):
    def prepared(self, **kwargs):
        s = Strategy(hello(), **kwargs)
        for i in range(90):
            self.assertEqual(s.on_batch(batch(i)), [])
        return s

    def test_discount_buys_half_equity(self):
        s = self.prepared()
        o = s.on_batch(batch(90, 9800000000))[0]
        self.assertEqual(o["side"], "buy")
        self.assertEqual(o["notional_quote_units"], "500000000")
        self.assertFalse(o["client_id"].startswith("_"))

    def test_gap_produces_no_order_or_history(self):
        s = self.prepared()
        b = batch(90)
        b["bars"] = []
        before = list(s.closes)
        self.assertEqual(s.on_batch(b), [])
        self.assertEqual(list(s.closes), before)

    def test_warmup_disallows_trade(self):
        s = self.prepared()
        s.hello["warmup_bars"] = 100
        self.assertEqual(s.on_batch(batch(90, 9800000000)), [])

    def test_no_clock_phase_seed_or_timestamp_signal(self):
        a, b = self.prepared(), self.prepared()
        b.hello.update(seed="987654321", phase="different", first_close_ms=999)
        x, y = batch(90, 9800000000), batch(90, 9800000000)
        y["t"] += 123456780000
        y["bars"][0]["t"] += 123456780000
        self.assertEqual(a.on_batch(x), b.on_batch(y))

    def test_unseen_suffix_cannot_change_prefix(self):
        h = hello()
        prices = [10000000000] * 100 + [9800000000] * 4
        bars = [bar(p, (i + 1) * 60000) for i, p in enumerate(prices)]
        a = replay(h, bars + [bar(10500000000, 105 * 60000)], Strategy(h).on_batch)[2]
        b = replay(h, bars + [bar(9000000000, 105 * 60000)], Strategy(h).on_batch)[2]
        self.assertEqual(a.splitlines()[:103], b.splitlines()[:103])

    def test_take_profit_sells_actual_qty(self):
        s = self.prepared()
        b = batch(90, 10200000000)
        b["account"]["positions"] = [{"symbol": "CBBTC", "qty": "500000", "cost_quote_units": "500000000"}]
        o = s.on_batch(b)[0]
        self.assertEqual((o["side"], o["qty"]), ("sell", "500000"))

    def test_stop_sells(self):
        s = self.prepared()
        b = batch(90, 9700000000)
        b["account"]["positions"] = [{"symbol": "CBBTC", "qty": "500000", "cost_quote_units": "500000000"}]
        self.assertEqual(s.on_batch(b)[0]["side"], "sell")

    def test_low_cash_and_zero_position_cap(self):
        for mode in ("cash", "cap"):
            s = self.prepared()
            b = batch(90, 9800000000)
            if mode == "cash":
                b["account"]["cash_quote_units"] = "999999"
            else:
                s.hello["caps"]["max_open_positions"] = 0
            self.assertEqual(s.on_batch(b), [])

    def test_thin_depth_raises_entry_hurdle(self):
        s = self.prepared()
        b = batch(90, 9800000000)
        b["bars"][0]["c_depth"] = ["1000000000"]
        self.assertEqual(s.on_batch(b), [])


class FillTests(unittest.TestCase):
    def test_integer_buy_sell_and_cash_conservation(self):
        r = Replay(hello())
        buy = {"side": "buy", "client_id": "a", "notional_quote_units": "500000000", "max_slippage_bps": 100}
        fill = r.execute(buy, bar(), 10000000000, 1)
        self.assertEqual(fill["status"], "filled")
        self.assertEqual(r.cash, 500000000)
        self.assertEqual(fill["fee_quote_units"], "200000")
        spend = 500000000 * 9996 // 10000
        expected_price = 10000000000 * 10002 * (1000000000000 + spend) // (10000 * 1000000000000)
        self.assertEqual(int(fill["price"]), expected_price)
        sell = {"side": "sell", "client_id": "b", "qty": str(r.qty), "max_slippage_bps": 100}
        r.execute(sell, bar(), 10000000000, 2)
        self.assertEqual(r.qty, 0)
        self.assertLess(r.cash, r.start)
        self.assertGreater(r.fees, 200000)

    def test_max_slippage_rejection_preserves_cash(self):
        r = Replay(hello())
        order = {"side": "buy", "client_id": "a", "notional_quote_units": "500000000", "max_slippage_bps": 0}
        fill = r.execute(order, bar(), 10000000000, 1)
        self.assertEqual(fill["reason"], "max_slippage")
        self.assertEqual((r.cash, r.qty, r.fees), (r.start, 0, 0))

    def test_forced_flatten_charges_sell_costs(self):
        h = hello()
        n = 0
        def buy_once(b):
            nonlocal n
            n += 1
            return [{"side": "buy", "client_id": "a", "symbol": "CBBTC", "notional_quote_units": "500000000", "max_slippage_bps": 100}] if n == 1 else []
        result, _, _, book = replay(h, [bar(t=60000), bar(t=120000)], buy_once)
        self.assertEqual(book.qty, 0)
        self.assertEqual(result["trades"], 1)
        self.assertLess(result["net_pnl_quote_units"], 0)
        self.assertEqual(book.resolutions[-1]["client_id"], "_flatten_0")


if __name__ == "__main__":
    unittest.main()
