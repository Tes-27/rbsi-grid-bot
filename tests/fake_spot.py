# -*- coding: utf-8 -*-
"""Deribit spot จำลอง (interface เดียวกับ DeribitClient ที่บอท Fixed Value ใช้)
จงใจทำให้ข้อมูลไม่พร้อมกัน: open orders เห็นทันที แต่ balance / trades เห็นช้าไป lag ขั้น
fee เก็บในสกุลที่ได้รับ (ขาย → USDC · ซื้อ → BTC) เพื่อทดสอบบัญชีไม้ทั้งสองสกุล
กรอบราคาแบบ Deribit (min_price/max_price ±band จากราคา): ออเดอร์ที่ค้างนอกกรอบถูก exchange ยกเลิกเอง (เจอจริง 29 ก.ย. 2026)"""
from __future__ import annotations
import asyncio, itertools, random
from rbsi_bot.deribit import RpcError, RpcTimeout

TICK = 0.01


class FakeSpot:
    def __init__(self, price, base=0.0, quote=0.0, fee=0.0002, bal_lag=0, trade_lag=0, timeout_prob=0.0, partial_prob=0.0, seed=0, band=0.0):
        self.bid = round(float(price), 2)
        self.base, self.quote, self.fee = float(base), float(quote), fee
        self.orders, self.states, self.trades = {}, {}, []
        self.step, self.t = 0, 1_700_000_000.0
        self.hist = {0: (self.base, self.quote)}
        self.bal_lag, self.trade_lag = bal_lag, trade_lag
        self.timeout_prob, self.partial_prob = timeout_prob, partial_prob
        self.rng = random.Random(seed)
        self.ids, self.tids = itertools.count(1), itertools.count(1)
        self.on_event, self.closed = None, asyncio.Event()
        self.placed = 0
        self.band = band
        self.band_cancels = 0

    @property
    def limits(self):
        return (round(self.bid * (1 - self.band), 2), round(self.bid * (1 + self.band), 2)) if self.band else (None, None)

    @property
    def ask(self):
        return round(self.bid + TICK, 2)

    # ---------- เวลา ----------
    def now(self):
        return self.t

    async def sleep(self, sec):
        self.advance(sec)

    def advance(self, sec=1.0):
        self.t += max(sec, 0.001)
        self.step += 1
        self.hist[self.step] = (self.base, self.quote)

    # ---------- ตลาด ----------
    def _fill(self, o, amount):
        amount = round(min(amount, o["amount"] - o["filled_amount"]), 8)
        if amount <= 0:
            return
        p = o["price"]
        o["filled_amount"] = round(o["filled_amount"] + amount, 8)
        if o["direction"] == "sell":
            fee = amount * p * self.fee
            self.base -= amount
            self.quote += amount * p - fee
            fc = "USDC"
        else:
            fee = amount * self.fee
            self.base += amount - fee
            self.quote -= amount * p
            fc = "BTC"
        self.trades.append({"trade_id": f"T{next(self.tids)}", "order_id": o["order_id"], "timestamp": int(self.t * 1000),
                            "direction": o["direction"], "amount": amount, "price": p, "label": o.get("label", ""),
                            "fee": fee, "fee_currency": fc, "_step": self.step})
        if o["filled_amount"] >= o["amount"] - 1e-12:
            o["order_state"] = "filled"
            self.orders.pop(o["order_id"], None)
        if self.on_event:
            self.on_event({"channel": "user.changes"})

    def move_price(self, new):
        """ราคา (bid) วิ่งไป new · ออเดอร์ที่ราคาผ่านถูก fill (บางครั้งครึ่งเดียว)"""
        old, new = self.bid, round(float(new), 2)
        self.bid = new
        for o in sorted(list(self.orders.values()), key=lambda o: o["price"], reverse=new < old):
            if o["order_id"] not in self.orders:
                continue
            hit = (o["direction"] == "sell" and new >= o["price"]) or (o["direction"] == "buy" and round(new + TICK, 2) <= o["price"])
            if hit:
                rem = o["amount"] - o["filled_amount"]
                part = rem / 2 if self.rng.random() < self.partial_prob and rem >= 2e-8 else rem
                self._fill(o, max(1e-8, round(part, 8)))
        lo, hi = self.limits
        if lo:
            for o in list(self.orders.values()):                     # นอกกรอบราคา → exchange ยกเลิกเอง
                if not (lo <= o["price"] <= hi):
                    o["order_state"] = "cancelled"
                    self.orders.pop(o["order_id"], None)
                    self.band_cancels += 1
        self.advance(0.5)

    def deposit(self, base=0.0, quote=0.0):
        self.base += base
        self.quote += quote
        self.advance(0.1)

    # ---------- API ----------
    async def connect(self):
        self.closed.clear()

    async def close(self):
        self.closed.set()

    async def subscribe(self, ch):
        return ch

    async def rpc(self, method, params=None):
        raise RpcError(method, {"message": "not supported in fake", "code": -1})

    async def account_summary(self, cur):
        b, q = self.hist.get(max(0, self.step - self.bal_lag), (self.base, self.quote))
        return {"balance": b if cur == "BTC" else q, "currency": cur}

    async def ticker(self, ins):
        lo, hi = self.limits
        return {"best_bid_price": self.bid, "best_ask_price": self.ask, "mark_price": self.bid * 1.002, "min_price": lo, "max_price": hi}

    async def open_orders(self, ins):
        return [dict(o) for o in self.orders.values()]

    async def order_state(self, oid):
        if oid not in self.states:
            raise RpcError("private/get_order_state", {"message": "order_not_found", "code": 10004})
        return dict(self.states[oid])

    async def trades_since(self, ins, start_ms):
        return [dict(t) for t in self.trades if t["_step"] <= self.step - self.trade_lag and t["timestamp"] >= start_ms]

    async def place(self, side, ins, amount, *, price=None, label, reduce_only=False, post_only=True):
        assert price is not None and post_only and not reduce_only
        assert abs(round(amount, 8) - amount) < 1e-12 and abs(round(price, 2) - price) < 1e-9, (amount, price)
        if (side == "sell" and price <= self.bid) or (side == "buy" and price >= self.ask):
            raise RpcError("private/" + side, {"message": "post_only_reject", "code": 11054})
        res_s = sum(o["amount"] - o["filled_amount"] for o in self.orders.values() if o["direction"] == "sell")
        res_b = sum((o["amount"] - o["filled_amount"]) * o["price"] for o in self.orders.values() if o["direction"] == "buy")
        if (side == "sell" and amount > self.base - res_s + 1e-12) or (side == "buy" and amount * price > self.quote - res_b + 1e-9):
            raise RpcError("private/" + side, {"message": "not_enough_funds", "code": 10009})
        oid = f"S{next(self.ids)}"
        o = {"order_id": oid, "direction": side, "amount": float(amount), "filled_amount": 0.0, "label": label,
             "price": float(price), "order_state": "open", "reduce_only": False}
        self.orders[oid] = self.states[oid] = o
        self.placed += 1
        if self.rng.random() < self.timeout_prob:
            raise RpcTimeout("private/" + side + " timeout (จำลอง)")
        return {"order": dict(o)}

    async def cancel(self, oid):
        o = self.orders.pop(oid, None)
        if o is None:
            raise RpcError("private/cancel", {"message": "order_not_found", "code": 10004})
        o["order_state"] = "cancelled"
        return dict(o)
