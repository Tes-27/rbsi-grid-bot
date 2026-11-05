# -*- coding: utf-8 -*-
"""Exchange จำลองสำหรับทดสอบ (interface เดียวกับ DeribitClient)
จงใจทำให้ข้อมูลไม่พร้อมกัน: open orders เห็นทันที แต่ position / trades เห็นช้าไป lag ขั้น
(สถานการณ์ที่ทำให้ "ออเดอร์หายแต่ position ยังไม่อัปเดต" → เสี่ยงวางซ้ำ)"""
from __future__ import annotations
import asyncio, itertools, random
from rbsi_bot.deribit import RpcError, RpcTimeout


class FakeExchange:
    def __init__(self, price, pos_lag=0, trade_lag=0, timeout_prob=0.0, partial_prob=0.0, seed=0, equity=0.01, rate_limit=0,
                 order_lag=0, purge_cancelled=False, max_open=100):
        self.price = float(price)
        self.pos = 0.0
        self.inv = 0.0                      # Σ q/p ของ position ที่ถือ (short หรือ long) → avg entry แบบ harmonic
        self.orders = {}                    # oid -> dict (open)
        self.states = {}                    # oid -> dict (ทุกออเดอร์)
        self.trades = []
        self.step = 0
        self.pos_hist = {0: 0.0}
        self.pos_lag, self.trade_lag = pos_lag, trade_lag
        self.timeout_prob, self.partial_prob = timeout_prob, partial_prob
        self.rng = random.Random(seed)
        self.ids = itertools.count(1)
        self.tids = itertools.count(1)
        self.t = 1_700_000_000.0
        self.equity = equity
        self.on_event = None
        self.closed = asyncio.Event()
        self.placed = 0
        self.rate_limit = rate_limit            # คำสั่ง place ต่อวินาที (0 = ไม่จำกัด)
        self._win = (0, 0)
        self.throttled = 0
        self.order_lag = order_lag              # open orders เห็นช้า (เจอจริง 7 ต.ค. 2026: position อัปเดตแล้วแต่ออเดอร์ที่ fill ยังค้างในรายการ)
        self.orders_hist = {0: {}}
        self.purge_cancelled = purge_cancelled  # Deribit: ออเดอร์ที่ยกเลิกโดยไม่มี fill → get_order_state ตอบ order_not_found
        self.max_open = max_open                # Deribit: ออเดอร์ค้างได้ไม่เกิน 100 ตัวต่อสัญญา (11020)
        self.max_open_rejects = 0
        self.max_open_seen = 0

    # ---------- เวลา ----------
    def now(self):
        return self.t

    async def sleep(self, sec):
        self.advance(sec)

    def advance(self, sec=1.0):
        self.t += max(sec, 0.001)
        self.step += 1
        self.pos_hist[self.step] = self.pos
        if self.order_lag:
            self.orders_hist[self.step] = {k: dict(v) for k, v in self.orders.items()}

    # ---------- ตลาด ----------
    def _fill(self, o, amount, price):
        amount = min(amount, o["amount"] - o["filled_amount"])
        if o.get("reduce_only"):                    # reduce-only ลดได้ไม่เกิน position ฝั่งตรงข้าม (พลิกฝั่งไม่ได้)
            amount = min(amount, max(0.0, -self.pos if o["direction"] == "buy" else self.pos))
        if amount <= 0:
            return 0.0
        o["filled_amount"] += amount
        sgn = -1 if o["direction"] == "sell" else 1
        if self.pos * sgn >= 0:                     # เพิ่ม position ฝั่งเดิม (หรือเปิดจาก 0)
            self.inv += amount / price
        else:                                       # ลด position (คิดตาม avg entry)
            self.inv -= self.inv * (amount / abs(self.pos))
        self.pos += sgn * amount
        self.trades.append({"trade_id": f"T{next(self.tids)}", "order_id": o["order_id"], "timestamp": int(self.t * 1000),
                            "direction": o["direction"], "amount": amount, "price": price, "label": o.get("label", ""),
                            "_step": self.step})
        flat = self.pos >= 0 if o["direction"] == "buy" else self.pos <= 0
        if o["filled_amount"] >= o["amount"] - 1e-9 or (o.get("reduce_only") and flat):
            o["order_state"] = "filled" if o["filled_amount"] >= o["amount"] - 1e-9 else "cancelled"
            self.orders.pop(o["order_id"], None)
        if self.on_event:
            self.on_event({"channel": "user.changes"})
        return amount

    def move_price(self, new):
        """ราคาวิ่งจากราคาเดิมไป new · fill limit ที่ถูกแตะ (บางครั้ง fill ครึ่งเดียว)"""
        old, new = self.price, float(new)
        self.price = new
        book = sorted(self.orders.values(), key=lambda o: o["price"], reverse=new < old)
        for o in list(book):
            if o["order_id"] not in self.orders:
                continue
            hit = (o["direction"] == "sell" and old < o["price"] <= new) or (o["direction"] == "buy" and new <= o["price"] < old)
            if hit:
                rem = o["amount"] - o["filled_amount"]
                part = rem / 2 if self.rng.random() < self.partial_prob and rem >= 20 else rem
                part = max(10.0, round(part / 10) * 10)
                self._fill(o, part, o["price"])
        self.advance(0.5)

    # ---------- API ----------
    async def connect(self):
        self.closed.clear()

    async def close(self):
        self.closed.set()

    async def subscribe(self, ch):
        return ch

    async def ticker(self, ins):
        return {"mark_price": self.price, "best_bid_price": self.price - 0.5, "best_ask_price": self.price + 0.5, "index_price": self.price}

    async def position(self, ins):
        vis = self.pos_hist.get(max(0, self.step - self.pos_lag), 0.0)
        mm = abs(vis) / self.price * 0.0126
        avg = (abs(vis) / self.inv) if vis != 0 and self.inv > 0 else 0.0
        return {"size": vis, "mark_price": self.price, "average_price": avg, "maintenance_margin": mm}

    async def open_orders(self, ins):
        if self.order_lag:
            return [dict(o) for o in self.orders_hist.get(max(0, self.step - self.order_lag), {}).values()]
        return [dict(o) for o in self.orders.values()]

    async def account_summary(self, cur="BTC"):
        return {"equity": self.equity, "maintenance_margin": abs(self.pos) / self.price * 0.0126, "available_funds": self.equity}

    async def order_state(self, oid):
        if oid not in self.states:
            raise RpcError("private/get_order_state", {"message": "order_not_found", "code": 10004})
        return dict(self.states[oid])

    async def trades_since(self, ins, start_ms):
        return [dict(t) for t in self.trades if t["_step"] <= self.step - self.trade_lag and t["timestamp"] >= start_ms]

    async def place(self, side, ins, amount, *, price=None, label, reduce_only=False, post_only=True):
        if self.rate_limit:
            sec = int(self.t)
            cnt = self._win[1] + 1 if self._win[0] == sec else 1
            self._win = (sec, cnt)
            if cnt > self.rate_limit:
                self.throttled += 1
                raise RpcError("private/" + side, {"message": "too_many_requests", "code": 10028})
        if price is not None and self.max_open and len(self.orders) >= self.max_open:
            self.max_open_rejects += 1
            raise RpcError("private/" + side, {"message": f"max_open_orders_per_instrument {self.max_open}", "code": 11020})
        oid = f"O{next(self.ids)}"
        o = {"order_id": oid, "direction": side, "amount": float(amount), "filled_amount": 0.0, "label": label,
             "reduce_only": reduce_only, "price": price if price is not None else "market_price", "order_state": "open"}
        if price is not None:
            crosses = (side == "sell" and price <= self.price - 0.5) or (side == "buy" and price >= self.price + 0.5)
            if crosses:
                raise RpcError("private/" + side, {"message": "post_only_reject", "code": 11054})
            o["price"] = float(price)
            self.orders[oid] = o
            self.states[oid] = o
            self.max_open_seen = max(self.max_open_seen, len(self.orders))
        else:
            self.states[oid] = o
            self.orders[oid] = o
            self._fill(o, o["amount"], self.price + (0.5 if side == "buy" else -0.5))
            if o["order_id"] in self.orders:           # reduce-only ที่ fill ไม่หมด → ยกเลิกที่เหลือ
                o["order_state"] = "cancelled"
                self.orders.pop(oid)
        self.placed += 1
        if self.rng.random() < self.timeout_prob:
            raise RpcTimeout("private/" + side + " timeout (จำลอง: ส่งถึงแล้วแต่คำตอบหาย)")
        return {"order": dict(o)}

    async def cancel(self, oid):
        o = self.orders.pop(oid, None)
        if o is None:
            raise RpcError("private/cancel", {"message": "order_not_found", "code": 10004})
        o["order_state"] = "cancelled"
        if self.purge_cancelled and o["filled_amount"] <= 0:
            self.states.pop(oid, None)
        return dict(o)

    # ---------- ตัวช่วยตรวจ ----------
    def own_open(self, zone_id, adopted=()):
        return [o for o in self.orders.values() if (o.get("label") or "").startswith(f"rbsi_{zone_id}_") or o["order_id"] in adopted]
