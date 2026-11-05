# -*- coding: utf-8 -*-
"""บอท Fixed Asset Value บน Deribit spot (เช่น BTC_USDC) — ใช้ subaccount แยกที่บอทถือว่ายอดทั้งหมดเป็นของมัน

แหล่งความจริงที่ต้องตรงกันก่อนลงมือ:
  1) ยอด BTC ในบัญชี (balance)
  2) ยอดตามบัญชีไม้ = ยอดตอนเริ่ม + fill ของบอททุกไม้ (หัก fee ตามสกุลที่ Deribit เก็บ)
  ไม่ตรง = ข้อมูลยังไม่นิ่ง (รอ) · ค้างเกิน flux_halt_sec = BTC เปลี่ยนนอกบอท (โอนเข้า/ออก) → หยุดแล้วถาม (--accept-balance)
  USDC เปลี่ยนนอกบอท (โอนเข้า/ออก) → รับยอดใหม่ + แจ้ง ไม่หยุด (เงินสำรองมากขึ้นไม่กระทบกลยุทธ์)
ออเดอร์: ฝั่งละไม่เกิน 1 ตัว · ติด label fv_<id>_s / fv_<id>_b · ล็อกฝั่งที่เพิ่งส่ง (pending) กันส่งซ้ำตอนคำตอบหาย
เริ่มบอทตอนมูลค่า BTC ห่าง V ≥ Δ → ไม่เริ่มจนกว่าผู้ใช้ยืนยัน (--rebalance-now)"""
from __future__ import annotations
import asyncio, logging, time
from dataclasses import dataclass

from .fv import FVConfig, plan
from .bot import to_order, Halt
from .store import Store
from .deribit import RpcError, RpcTimeout

log = logging.getLogger("fvbot")


@dataclass
class FVSafety:
    settle_sec: float = 0.3
    debounce_sec: float = 0.1
    reconcile_sec: float = 30
    pending_ttl: float = 60
    flux_halt_sec: float = 180
    heartbeat_sec: float = 300
    summary_hours: float = 12
    order_spacing_sec: float = 0.15
    throttle_max_sec: float = 30
    cash_accept_sec: float = 10          # USDC ต่างจากบัญชีไม้ค้างนานเท่านี้ (และ BTC ตรง) → ถือว่าโอนเข้า/ออก
    rebalance_now: bool = False          # ยืนยันให้ปรับเข้า V ตอนเริ่ม (มูลค่าห่าง V ≥ Δ)
    accept_balance: bool = False         # ยืนยันยอด BTC ใหม่ที่เปลี่ยนนอกบอท


class Refuse(Exception):
    """ไม่เริ่มบอท (ยังไม่บันทึกอะไร)"""


class FVStore(Store):
    def _load(self):
        d = super()._load()
        if "kind" not in d:                                   # ไฟล์ใหม่ → โครงของ fixed value
            d = {"kind": "fv", "zone_id": self.zone_id, "status": "new", "halt_reason": "", "created": int(time.time() * 1000),
                 "ledger_start_ms": None, "exp": {"base": 0.0, "quote": 0.0}, "start": {}, "trade_ids": [], "last_ts": 0,
                 "by_order": {}, "known": {}, "foreign_trade_ids": [], "fills": [], "fees_usd": 0.0, "heartbeat": {}}
        return d


class FVBot:
    def __init__(self, cfg: FVConfig, client, store: FVStore, notifier, live: bool, safety: FVSafety | None = None,
                 sleep=asyncio.sleep, now=time.time):
        self.cfg, self.c, self.store, self.n = cfg, client, store, notifier
        self.live, self.sf = live, safety or FVSafety()
        self.sleep, self.now = sleep, now
        self.pending: dict[str, float] = {}
        self.flux_since = None
        self.cash_diff = None                 # (ยอดต่าง, เวลาเริ่มเห็น)
        self.wake = asyncio.Event()
        self.throttle_until = self.throttle_sec = 0.0
        self.last_hb_log = self.last_summary = 0.0
        self.last_dry = None
        self.started_at = now()
        self.last = None

    # ---------- อ่าน ----------
    def is_own(self, label, order_id) -> str | None:
        """'sell' / 'buy' ถ้าเป็นของบอท"""
        if label and label.startswith(f"fv_{self.cfg.fv_id}_"):
            return "sell" if label.endswith("_s") else "buy"
        return self.store.data["known"].get(order_id)

    async def read(self):
        b, q, tk, oo = await asyncio.gather(self.c.account_summary(self.cfg.base), self.c.account_summary(self.cfg.quote),
                                            self.c.ticker(self.cfg.instrument), self.c.open_orders(self.cfg.instrument))
        bid, ask = tk.get("best_bid_price"), tk.get("best_ask_price")
        lo, hi = tk.get("min_price"), tk.get("max_price")
        return {"base": float(b.get("balance") or 0), "quote": float(q.get("balance") or 0), "bid": float(bid or 0), "ask": float(ask or 0),
                "mark": float(tk.get("mark_price") or 0), "orders": [to_order(o) for o in oo],
                "band": (float(lo), float(hi)) if lo and hi else None}

    @staticmethod
    def _sig(r):
        return (round(r["base"], 10), round(r["quote"], 6), tuple(sorted((o.order_id, o.filled, o.amount, o.price) for o in r["orders"])))

    async def stable_read(self):
        r1 = await self.read()
        await self.sleep(self.sf.settle_sec)
        r2 = await self.read()
        return r2 if self._sig(r1) == self._sig(r2) else None

    # ---------- บัญชีไม้ ----------
    def apply_trade(self, t: dict, side: str):
        d = self.store.data
        amt, px = float(t["amount"]), float(t["price"])
        sgn = 1 if t["direction"] == "buy" else -1
        d["exp"]["base"] += sgn * amt
        d["exp"]["quote"] -= sgn * amt * px
        fee, fc = float(t.get("fee") or 0), (t.get("fee_currency") or self.cfg.quote).upper()
        d["exp"]["base" if fc == self.cfg.base else "quote"] -= fee
        fee_usd = fee * px if fc == self.cfg.base else fee
        d["fees_usd"] = d.get("fees_usd", 0.0) + fee_usd
        d["by_order"][t["order_id"]] = d["by_order"].get(t["order_id"], 0.0) + amt
        d["trade_ids"] = (d["trade_ids"] + [t["trade_id"]])[-5000:]
        d["last_ts"] = max(d["last_ts"], int(t["timestamp"]))
        return fee_usd

    async def sync_trades(self):
        d = self.store.data
        start = max(d["ledger_start_ms"] or 0, d["last_ts"] - 5000)
        trades = await self.c.trades_since(self.cfg.instrument, start)
        seen, seen_f = set(d["trade_ids"]), set(d["foreign_trade_ids"])
        new, foreign = [], []
        for t in trades:
            if t["timestamp"] < (d["ledger_start_ms"] or 0) or t["trade_id"] in seen:
                continue
            side = self.is_own(t.get("label"), t.get("order_id"))
            if side:
                fee = self.apply_trade(t, side)
                new.append((t, fee, self.fill_text(t, fee)))          # ข้อความ ณ ยอดหลังไม้นี้ (หลายไม้ในรอบเดียวก็ถูกลำดับ)
            elif t["trade_id"] not in seen_f:
                foreign.append(t)
        return new, foreign

    async def resolve_missing(self, orders):
        known, open_ids = self.store.data["known"], {o.order_id for o in orders}
        unresolved = set()
        for oid, side in list(known.items()):
            if oid in open_ids:
                continue
            try:
                st = await self.c.order_state(oid)
            except RpcError as e:
                if getattr(e, "code", None) == 10004 and self.store.data["by_order"].get(oid, 0.0) < 1e-12:
                    # Deribit ลบออเดอร์ที่ยกเลิกโดยไม่มี fill → order_not_found · ไม่มี fill ในบัญชีไม้ = ยกเลิกแล้ว
                    log.info("ออเดอร์ %s (%s) ไม่อยู่ในระบบ Deribit แล้ว และไม่มี fill → ถือว่ายกเลิกแล้ว", oid, side)
                    del known[oid]
                    self.pending.pop(side, None)
                    continue
                log.warning("order_state %s: %s", oid, e)
                unresolved.add(side)
                continue
            if st.get("order_state") in ("filled", "cancelled", "rejected") and \
                    abs(self.store.data["by_order"].get(oid, 0.0) - float(st.get("filled_amount") or 0)) < 1e-10:
                del known[oid]
                self.pending.pop(side, None)
            else:
                unresolved.add(side)
        return unresolved

    # ---------- จุดที่บอทชนิดอื่น (KZM) เปลี่ยนได้ ----------
    def direction(self, key: str) -> str:
        """ฝั่งของคำสั่งจาก key ของออเดอร์ (Fixed Value: key = ฝั่ง)"""
        return key

    def title(self) -> str:
        return f"Fixed Value {self.cfg.instrument} V ${self.cfg.value:,.0f} ± ${self.cfg.delta:,.2f}"

    # ---------- สถานะ / ข้อความ ----------
    def hodl_diff(self, price: float):
        """มูลค่าพอร์ตบอท − มูลค่าถ้าถือยอดตอนเริ่มเฉย ๆ (ณ ราคาเดียวกัน)"""
        s, e = self.store.data.get("start") or {}, self.store.data["exp"]
        if not s:
            return None
        return (e["base"] * price + e["quote"]) - (s["base"] * price + s["quote"])

    def fill_text(self, t, fee_usd):
        amt, px = float(t["amount"]), float(t["price"])
        e, cfg = self.store.data["exp"], self.cfg
        hd = self.hodl_diff(px)
        return (f"{'🔴 ขาย' if t['direction'] == 'sell' else '🟢 ซื้อ'} {amt:.8f} {cfg.base} @ {px:,.2f} = ${amt * px:,.2f} · fee ${fee_usd:,.4f}\n"
                f"{cfg.base} ${e['base'] * px:,.2f} (เป้า ${cfg.value:,.0f}) · {cfg.quote} {e['quote']:,.2f} · รวม ${e['base'] * px + e['quote']:,.2f}"
                + (f" · เทียบถือเฉย ๆ {hd:+,.2f}$" if hd is not None else ""))

    def status_text(self) -> str:
        d, hb, cfg = self.store.data, self.store.data.get("heartbeat") or {}, self.cfg
        st = {"active": "✅ ทำงานอยู่", "halted": "🛑 หยุด (halt)", "closed": "✅ ปิดแล้ว", "new": "⏳ ยังไม่เริ่ม"}.get(d["status"], d["status"])
        lines = [f"{st} · {'LIVE' if self.live else 'DRY-RUN'} · Fixed Value {cfg.instrument} V ${cfg.value:,.0f} ± ${cfg.delta:,.2f}"]
        if d.get("halt_reason"):
            lines.append(f"เหตุผล: {d['halt_reason']}")
        if hb:
            lines.append(f"ตรวจล่าสุด {self.now() - hb['ts']:,.0f} วิที่แล้ว · ราคา {hb['mid']:,.2f}")
            lines.append(f"{cfg.base} ${hb['value']:,.2f} ({hb['dev']:+,.2f}) · {cfg.quote} {hb['quote']:,.2f}")
            lines.append(f"ขายที่ {hb['sell_at'] or 0:,.2f} · ซื้อที่ {hb['buy_at'] or 0:,.2f} · ออเดอร์ของบอท {hb['own']}/2")
            if hb.get("hodl") is not None:
                lines.append(f"เทียบถือเฉย ๆ {hb['hodl']:+,.2f}$ · fee สะสม ${d.get('fees_usd', 0):,.2f}")
        lines.append(f"รันมา {(self.now() - self.started_at) / 3600:,.1f} ชม. · fill ทั้งหมด {len(d['fills'])} ครั้ง")
        return "\n".join(lines)

    async def tg_loop(self):
        """/status รวมทุกบอท (ดึงข้อความตัวเดียวผ่าน lock) + สรุปตามรอบ · ดู tghub.py"""
        from .tghub import tg_loop
        await tg_loop(self)

    # ---------- เริ่ม ----------
    async def startup(self):
        """ตรวจก่อนเริ่ม (อ่านอย่างเดียว ยกเว้นบันทึกสถานะเมื่อผ่าน) · ไม่ผ่าน = Refuse"""
        r = await self.stable_read()
        if r is None:
            raise Refuse("ข้อมูลไม่นิ่งตอนเริ่ม ลองใหม่")
        if not (r["bid"] > 0 and r["ask"] > r["bid"]):
            raise Refuse(f"order book ของ {self.cfg.instrument} ไม่ปกติ (bid {r['bid']} / ask {r['ask']})")
        foreign = [o for o in r["orders"] if not self.is_own(o.label, o.order_id)]
        if foreign:
            raise Refuse(f"มีออเดอร์ {self.cfg.instrument} ที่ไม่ใช่ของบอท {len(foreign)} ตัว — ใช้ subaccount แยกสำหรับบอทนี้ หรือยกเลิกออเดอร์เหล่านั้นก่อน")
        d, notes = self.store.data, []
        mid = (r["bid"] + r["ask"]) / 2
        if d["status"] == "new":
            d["exp"] = {"base": r["base"], "quote": r["quote"]}
            d["start"] = {"ts": int(self.now() * 1000), "mid": mid, "base": r["base"], "quote": r["quote"]}
            d["ledger_start_ms"] = int(self.now() * 1000)
            notes.append(f"เริ่มนับ: {r['base']:.8f} {self.cfg.base} + {r['quote']:,.2f} {self.cfg.quote}")
        else:
            if d["status"] == "closed":
                raise Refuse("บอทนี้ปิดไปแล้ว")
            new, foreign_t = await self.sync_trades()
            for t, fee, text in new:
                notes.append("ระหว่างบอทหยุด: " + text.split("\n")[0])
                self.log_fill(t, fee)
            if foreign_t:
                raise Refuse(f"มีการเทรด {self.cfg.instrument} ที่ไม่ใช่ของบอท {len(foreign_t)} รายการระหว่างบอทหยุด — ตรวจบัญชีแล้วเริ่มด้วย --accept-balance")
            db = r["base"] - d["exp"]["base"]
            if abs(db) > 1e-7:
                if not self.sf.accept_balance:
                    raise Refuse(f"ยอด {self.cfg.base} ในบัญชี {r['base']:.8f} ≠ บัญชีไม้ {d['exp']['base']:.8f} (ต่าง {db:+.8f}) "
                                 "— มีการโอนเข้า/ออก? ตรวจแล้วเริ่มด้วย --accept-balance")
                d["start"]["base"] += db                           # ถือเป็นเงินโอน: ไม่นับเป็นกำไร/ขาดทุนเทียบถือเฉย ๆ
                notes.append(f"รับยอด {self.cfg.base} ใหม่ ({db:+.8f})")
                d["exp"]["base"] = r["base"]
            dq = r["quote"] - d["exp"]["quote"]
            if abs(dq) > 0.01:
                d["start"]["quote"] += dq
                d["exp"]["quote"] = r["quote"]
                notes.append(f"{self.cfg.quote} เปลี่ยนนอกบอท {dq:+,.2f}")
        dev = r["base"] * mid - self.cfg.value
        if abs(dev) >= self.cfg.delta - 1e-9 and not self.sf.rebalance_now:
            side = "ขาย" if dev > 0 else "ซื้อ"
            raise Refuse(f"มูลค่า {self.cfg.base} ตอนนี้ ${r['base'] * mid:,.2f} ห่างเป้า ${self.cfg.value:,.0f} อยู่ {dev:+,.2f}$ (≥ Δ ${self.cfg.delta:,.2f}) "
                         f"— ต้อง{side}ราว ${abs(dev):,.2f} ให้กลับเข้าเป้า · ยืนยันด้วย --rebalance-now (post-only ที่ราคาดีที่สุดใน book)")
        if dev <= -self.cfg.delta and r["quote"] < abs(dev):
            notes.append(f"⚠️ {self.cfg.quote} {r['quote']:,.2f} ไม่พอซื้อให้ครบเป้า (ต้องการ ~{abs(dev):,.2f})")
        d["status"] = "active"
        d["halt_reason"] = ""
        if self.live:
            self.store.save()
        return r, notes

    def log_fill(self, t, fee_usd):
        self.store.data["fills"] = (self.store.data["fills"] + [{"ts": t["timestamp"], "side": t["direction"], "price": float(t["price"]),
                                    "amount": float(t["amount"]), "fee_usd": fee_usd, "trade_id": t["trade_id"]}])[-500:]

    # ---------- วงรอบหลัก ----------
    async def _flux(self, reason):
        now = self.now()
        self.flux_since = self.flux_since or now
        log.info("รอข้อมูลนิ่ง: %s", reason)
        if now - self.flux_since > self.sf.flux_halt_sec:
            return await self.halt(f"ข้อมูลไม่นิ่งเกิน {self.sf.flux_halt_sec:.0f} วิ: {reason}")
        self.wake.set()

    async def reconcile(self):
        d = self.store.data
        if d["status"] != "active":
            return
        r = await self.stable_read()
        if r is None:
            return await self._flux("สถานะเปลี่ยนระหว่างอ่าน 2 รอบ")
        if not (r["bid"] > 0 and r["ask"] > r["bid"]):
            return await self._flux(f"order book ไม่ปกติ (bid {r['bid']} / ask {r['ask']})")
        new, foreign = await self.sync_trades()
        for t, fee, text in new:
            self.log_fill(t, fee)
            self.n.post(text)
        if foreign:
            d["foreign_trade_ids"] = (d["foreign_trade_ids"] + [t["trade_id"] for t in foreign])[-500:]
            return await self.halt(f"มีการเทรด {self.cfg.instrument} ที่ไม่ใช่ของบอท {len(foreign)} รายการ (เทรดมือ?)")
        foreign_o = [o for o in r["orders"] if not self.is_own(o.label, o.order_id)]
        if foreign_o:
            return await self.halt(f"มีออเดอร์ {self.cfg.instrument} ที่ไม่ใช่ของบอท {len(foreign_o)} ตัว (วางมือ?)")
        for o in r["orders"]:
            d["known"].setdefault(o.order_id, self.is_own(o.label, o.order_id))
        unresolved = await self.resolve_missing(r["orders"])
        if unresolved:
            return await self._flux(f"รอยืนยันออเดอร์ที่หายไปฝั่ง {sorted(unresolved)}")
        for o in r["orders"]:                                     # ยอด fill ของออเดอร์ที่เปิดอยู่ต้องเข้าบัญชีไม้แล้ว
            if abs(d["by_order"].get(o.order_id, 0.0) - o.filled) > 1e-10:
                return await self._flux(f"fill ของออเดอร์ {o.side} ยังไม่เข้าบัญชีไม้")
        db = r["base"] - d["exp"]["base"]
        if abs(db) > 1e-7:
            if self.flux_since and self.now() - self.flux_since > self.sf.flux_halt_sec:
                return await self.halt(f"ยอด {self.cfg.base} ในบัญชี ≠ บัญชีไม้ ({db:+.8f}) นานเกิน {self.sf.flux_halt_sec:.0f} วิ "
                                       "— มีการโอนเข้า/ออก? ตรวจแล้วเริ่มใหม่ด้วย --clear-halt --accept-balance")
            return await self._flux(f"ยอด {self.cfg.base} ≠ บัญชีไม้ ({db:+.8f})")
        self.flux_since = None
        dq = r["quote"] - d["exp"]["quote"]
        if abs(dq) > 0.01:                                         # BTC ตรงแล้ว แต่ USDC ต่าง → รอให้นิ่งก่อนถือว่าเป็นเงินโอน
            if self.cash_diff and abs(self.cash_diff[0] - dq) < 0.01:
                if self.now() - self.cash_diff[1] >= self.sf.cash_accept_sec:
                    d["exp"]["quote"] = r["quote"]
                    d["start"]["quote"] = d["start"].get("quote", 0.0) + dq
                    self.cash_diff = None
                    self.n.post(f"💵 {self.cfg.quote} เปลี่ยนนอกบอท {dq:+,.2f} (โอนเข้า/ออก?) — ใช้ยอดใหม่ {r['quote']:,.2f}", key="cash")
            else:
                self.cash_diff = (dq, self.now())
            return self.wake.set()
        self.cash_diff = None
        now = self.now()
        for side, t0 in list(self.pending.items()):
            if any(self.is_own(o.label, o.order_id) == side for o in r["orders"]) or now - t0 > self.sf.pending_ttl:
                self.pending.pop(side, None)
        await self.decide(r, now)

    async def decide(self, r, now):
        d = self.store.data
        own = [o for o in r["orders"] if self.is_own(o.label, o.order_id)]
        dec = plan(self.cfg, d["exp"]["base"], d["exp"]["quote"], r["bid"], r["ask"], own, pending=set(self.pending), band=r["band"])
        self.last = dec
        if dec.info.get("usdc_short"):
            self.n.post(f"⚠️ {self.cfg.quote} ไม่พอซื้อ (ต้องการ ~{dec.info['usdc_short']:,.2f} มี {d['exp']['quote']:,.2f}) — หยุดฝั่งซื้อ ฝั่งขายทำงานต่อ", key="short")
        i = dec.info
        d["heartbeat"] = {"ts": now, "mid": i["mid"], "value": i["value"], "dev": i["dev"], "quote": d["exp"]["quote"], "sell_at": i["sell_at"],
                          "buy_at": i["buy_at"], "own": len(own), "hodl": self.hodl_diff(i["mid"]), "live": self.live}
        if now - self.last_hb_log >= self.sf.heartbeat_sec:
            self.last_hb_log = now
            log.info("%s", f"✓ ทำงานปกติ · ราคา {i['mid']:,.2f} · {self.cfg.base} ${i['value']:,.2f} ({i['dev']:+,.2f}) · "
                           f"{self.cfg.quote} {d['exp']['quote']:,.2f} · ขาย {i['sell_at'] or 0:,.2f} / ซื้อ {i['buy_at'] or 0:,.2f} · ออเดอร์ {len(own)}/2")
        await self.execute(dec.actions)
        if self.live:
            self.store.save()

    def describe(self, a):
        if a[0] == "cancel":
            return f"ยกเลิก {a[2]} {a[1]} ({a[3]})"
        _, side, price, amt = a
        return f"{'SELL' if side == 'sell' else 'BUY'} {amt:.8f} {self.cfg.base} @ {price:,.2f} (${amt * price:,.2f})"

    async def execute(self, actions):
        if not actions:
            return
        if not self.live:
            sig = tuple(actions)
            if sig != self.last_dry:
                self.last_dry = sig
                for a in actions:
                    log.info("[DRY-RUN] %s", self.describe(a))
            return
        if self.now() < self.throttle_until:
            self.wake.set()
            return
        for i, a in enumerate(sorted(actions, key=lambda a: a[0] != "cancel")):     # ยกเลิกก่อนเสมอ
            if i:
                await self.sleep(self.sf.order_spacing_sec)
            try:
                if a[0] == "cancel":
                    await self.c.cancel(a[1])
                    log.info("%s", self.describe(a))
                    continue
                _, side, price, amt = a
                self.pending[side] = self.now()
                res = await self.c.place(self.direction(side), self.cfg.instrument, round(amt, 8), price=round(price, 2), label=self.cfg.label(side))
                oid = res["order"]["order_id"]
                self.store.data["known"][oid] = side
                log.info("%s", f"วาง {self.describe(a)} → {oid}")
                self.throttle_sec = 0.0
            except RpcTimeout as e:
                self.n.post(f"⚠️ ส่งคำสั่งแล้วไม่ได้คำตอบ ({e}) — ล็อกฝั่งนี้ไว้ {self.sf.pending_ttl:.0f} วิ แล้วตรวจใหม่", key="timeout")
            except RpcError as e:
                if a[0] != "cancel":
                    self.pending.pop(a[1], None)
                m = str(e).lower()
                if getattr(e, "code", None) in (10028, 10040, 10041, 13888) or "too_many" in m or "retry" in m:
                    self.throttle_sec = min(max(self.throttle_sec * 2, 1.0), self.sf.throttle_max_sec)
                    self.throttle_until = self.now() + self.throttle_sec
                    self.n.post(f"🐢 Deribit จำกัดความถี่ ({e}) — พัก {self.throttle_sec:.0f} วิ", key="throttle")
                    break
                self.n.post(f"⚠️ {self.describe(a)} ไม่ผ่าน: {e}", key=f"rej{a[1]}")
        self.wake.set()

    async def halt(self, reason):
        self.store.data.update(status="halted", halt_reason=reason)
        n = 0
        if self.live:
            try:
                for o in await self.c.open_orders(self.cfg.instrument):
                    if self.is_own(o.get("label"), o["order_id"]):
                        await self.c.cancel(o["order_id"])
                        n += 1
            except Exception as e:
                log.error("ยกเลิกออเดอร์ตอน halt ไม่สำเร็จ: %s", e)
            self.store.save()
        log.error("HALT: %s", reason)
        await self.n.flush()
        await self.n.send(f"🛑 HALT: {reason}\nยกเลิกออเดอร์ของบอท {n} ตัว · ตรวจแล้วรันใหม่ด้วย --clear-halt")

    async def close(self, reason):
        n = 0
        if self.live:
            for o in await self.c.open_orders(self.cfg.instrument):
                if self.is_own(o.get("label"), o["order_id"]):
                    await self.c.cancel(o["order_id"])
                    n += 1
        self.store.data["status"] = "closed"
        if self.live:
            self.store.save()
        await self.n.flush()
        await self.n.send(f"✅ ปิดบอท {self.title()}: {reason} · ยกเลิกออเดอร์ {n} ตัว")

    async def run(self):
        backoff, started = 2, False
        while True:
            try:
                self.c.on_event = lambda p: self.wake.set()
                await self.c.connect()
                await self.c.subscribe([f"user.changes.{self.cfg.instrument}.raw"])
                if not started:
                    r, notes = await self.startup()
                    asyncio.create_task(self.tg_loop())
                    await self.n.send(f"▶️ เริ่มบอท {'LIVE' if self.live else 'DRY-RUN'} · {self.title()}\n" + "\n".join(notes) + "\nพิมพ์ /status เพื่อดูสถานะ")
                    started = True
                backoff = 2
                while not self.c.closed.is_set():
                    self.wake.clear()
                    await self.reconcile()
                    if self.store.data["status"] in ("halted", "closed"):
                        await self.n.flush()
                        await self.c.close()
                        return self.store.data["status"]
                    try:
                        await asyncio.wait_for(self.wake.wait(), self.sf.reconcile_sec)
                        await self.sleep(self.sf.debounce_sec)
                    except asyncio.TimeoutError:
                        pass
            except (Refuse, Halt) as e:
                await self.n.flush()
                await self.n.send(f"🛑 เริ่มไม่ได้: {e}")
                await self.c.close()
                return "refused"
            except Exception as e:
                if not started and isinstance(e, RpcError) and getattr(e, "code", None) not in (10028, 10040, 10041, 13888):
                    await self.n.flush()
                    await self.n.send(f"🛑 เริ่มไม่ได้: {e}")
                    await self.c.close()
                    return "refused"
                if not isinstance(e, (ConnectionError, OSError, RpcTimeout, RpcError)):
                    log.exception("error ไม่คาดคิด")
                log.warning("%s", f"{type(e).__name__}: {e} — ต่อใหม่ใน {backoff} วิ")
                self.n.post(f"🔌 {type(e).__name__}: {e} — ต่อใหม่ใน {backoff} วิ", key="disconnect")
                try:
                    await self.c.close()
                except Exception:
                    pass
                await self.sleep(backoff)
                backoff = min(backoff * 2, 60)
