# -*- coding: utf-8 -*-
"""ตัวควบคุมหลัก: อ่านสถานะ → ตรวจคำตอบ → ลงมือ (หรือแค่ log ใน dry-run)

ด่านกัน double position (เช่น sell 84,000 fill แล้วบอทเข้าใจผิดว่าออเดอร์หาย แล้ววางซ้ำ):
  1) ชุดออเดอร์ที่ควรมีคำนวณจาก "ชั้นที่ถืออยู่" ไม่ใช่จาก "ออเดอร์ที่หายไป"
  2) บัญชีไม้รายชั้น (ledger จาก fill ที่ติด label) ต้องรวมได้เท่ากับ position พอดี ไม่งั้นรอ
  3) ออเดอร์ของบอทที่หายจากรายการ ต้องถามสถานะจาก Deribit และ (ถ้า fill) ต้องเห็นยอดใน ledger ก่อน ชั้นนั้นจึงจะวางใหม่ได้
  4) อ่านสถานะ 2 รอบห่างกันต้องเหมือนกัน · ชั้นที่เพิ่งส่งออเดอร์ถูกล็อกจนยืนยันได้
  5) ที่ถือ + ออเดอร์เปิดที่รอ ต้องไม่เกินเพดาน zone — เกิน = halt ทันที (ยกเลิกออเดอร์เปิด เก็บออเดอร์ปิด reduce-only ไว้)
zone ทิศ short (sell เปิด / buy ปิด) หรือ long (buy เปิด / sell ปิด) — ดู grid.py
ทุกคำสั่งเป็น limit post-only (reject_post_only) — ไม่มี market order เพื่อจ่าย fee แบบ maker เท่านั้น"""
from __future__ import annotations
import asyncio, logging, time
from dataclasses import dataclass

from .grid import ZoneConfig, Snapshot, Order, plan, parse_label, m_range, expected
from .liq import mm_rate, eq_eff, liq_short_after_fills, liq_long_after_fills
from .deribit import RpcError, RpcTimeout
from .tghub import tg_loop

log = logging.getLogger("bot")


@dataclass
class Safety:
    settle_sec: float = 0.3          # ระยะห่างการอ่านสถานะ 2 รอบ (เดิม 1.5 — ความปลอดภัยอยู่ที่ตรวจคำตอบ 3 ทาง ไม่ใช่ระยะรอ)
    debounce_sec: float = 0.1        # รอหลังได้ event ก่อนตรวจ (รวบ event ที่มาติด ๆ กัน · เดิม 0.7)
    flux_halt_sec: float = 180       # ข้อมูลไม่นิ่งนานเกินนี้ = halt
    pending_ttl: float = 60          # ล็อกชั้นหลังส่งออเดอร์ (กันส่งซ้ำตอนเน็ตหน่วง)
    reconcile_sec: float = 30        # ตรวจรอบปกติ
    liq_buffer: float = 500          # liq ตอน zone เต็มต้องห่างกรอบอย่างน้อยเท่านี้ (short: เหนือ upper · long: ใต้ lower)
    default_mmr: float = 0.0126      # MM rate เมื่อยังไม่มี position ให้สอบเทียบ
    adopt_manual: bool = False       # รับช่วง: ยกเลิกออเดอร์มือบน instrument นี้แล้ววางใหม่พร้อม label
    force: bool = False              # ข้ามด่าน liq ตอนเริ่ม (ไม่แนะนำ)
    max_orders_per_cycle: int = 6    # traffic หนัก: ส่งทีละไม่เกินเท่านี้ ที่เหลือรอบหน้า
    order_spacing_sec: float = 0.15  # เว้นระหว่างคำสั่ง ไม่ยิงพรวด
    throttle_max_sec: float = 30     # backoff สูงสุดเมื่อโดน rate limit
    heartbeat_sec: float = 300       # log "ทำงานปกติ" ทุกเท่านี้วินาที
    summary_hours: float = 12        # ส่งสรุปสถานะเข้า Telegram ทุกกี่ชั่วโมง (0 = ปิด)


class Halt(Exception):
    pass


def to_order(o: dict) -> Order:
    return Order(order_id=o["order_id"], side=o["direction"], price=float(o["price"]) if o.get("price") not in (None, "market_price") else 0.0,
                 amount=float(o["amount"]), filled=float(o.get("filled_amount") or 0), label=o.get("label") or "",
                 reduce_only=bool(o.get("reduce_only")))


class GridBot:
    def __init__(self, cfg: ZoneConfig, client, store, notifier, live: bool, safety: Safety | None = None,
                 sleep=asyncio.sleep, now=time.time):
        self.cfg, self.c, self.store, self.n = cfg, client, store, notifier
        self.live, self.sf = live, safety or Safety()
        self.sleep, self.now = sleep, now
        self.pending: dict[tuple, float] = {}      # (ชั้น, ฝั่ง) -> เวลาที่ส่ง
        self.flux_since: float | None = None
        self.last_dry = None
        self.wake = asyncio.Event()
        self.last_snap = None
        self.last_decision = None
        self.dry_ignore: set[str] = set()
        self.throttle_until = 0.0
        self.throttle_sec = 0.0
        self.last_hb_log = 0.0
        self.last_summary = 0.0
        self.started_at = now()          # dry-run: ออเดอร์มือที่ 'จะถูกยกเลิก' ตอนรับช่วง

    def level_of(self, label, order_id):
        """ชั้นของออเดอร์/fill ถ้าเป็นของบอท (ติด label หรือเป็นออเดอร์มือที่รับช่วงมา) ไม่งั้น None"""
        kind, k = parse_label(label, self.cfg.zone_id)
        if kind == "own":
            return k
        if kind == "foreign":
            return self.store.data.get("adopted", {}).get(order_id)
        return None

    # ---------- อ่านสถานะ ----------
    async def read(self):
        ins = self.cfg.instrument
        pos, oo, tk = await asyncio.gather(self.c.position(ins), self.c.open_orders(ins), self.c.ticker(ins))   # ยิงพร้อมกัน ~0.2 วิ แทน ~0.6
        orders = [to_order(o) for o in oo]
        if self.dry_ignore:
            orders = [o for o in orders if o.order_id not in self.dry_ignore]
        return {"pos": pos, "position": float(pos.get("size") or 0), "orders": orders, "mark": float(tk["mark_price"]),
                "bid": tk.get("best_bid_price"), "ask": tk.get("best_ask_price")}

    @staticmethod
    def _sig(r):
        return (round(r["position"], 6), tuple(sorted((o.order_id, o.filled, o.amount, o.price, o.side) for o in r["orders"])))

    async def stable_read(self):
        r1 = await self.read()
        await self.sleep(self.sf.settle_sec)
        r2 = await self.read()
        return r2 if self._sig(r1) == self._sig(r2) else None

    async def sync_ledger(self):
        """ดึง fill ใหม่เข้าบัญชีไม้ · คืน (fill ใหม่ของบอท, fill ที่ไม่ใช่ของบอท)"""
        d = self.store.data
        start = max(d["ledger_start_ms"] or 0, d["ledger"]["last_ts"] - 5000)
        trades = await self.c.trades_since(self.cfg.instrument, start)
        new, foreign = [], []
        seen_foreign = set(d.setdefault("foreign_trade_ids", []))
        seen_own = set(d["ledger"]["trade_ids"])
        for t in trades:
            if t["timestamp"] < (d["ledger_start_ms"] or 0) or t["trade_id"] in seen_own:
                continue                                   # fill ที่บันทึกแล้ว (ดึงซ้อนเวลาเผื่อไว้)
            k = self.level_of(t.get("label"), t.get("order_id"))
            if k is not None and 1 <= k <= self.cfg.n:
                if self.store.add_trade(t, k, self.cfg.open_side):
                    new.append((t, k))
            elif t["trade_id"] not in seen_foreign:
                foreign.append(t)
        return new, foreign

    async def resolve_missing(self, orders):
        """ออเดอร์ของบอทที่หายจาก open orders → ต้องรู้ผลแน่ชัด (filled/cancelled/rejected) และ ledger ตรงกับยอด fill"""
        known = self.store.data["known_orders"]
        open_ids = {o.order_id for o in orders}
        for o in orders:
            k = self.level_of(o.label, o.order_id)
            if k is not None and o.order_id not in known:
                known[o.order_id] = [k, o.side, o.amount]
        unresolved = set()
        for oid, (k, side, amt) in list(known.items()):
            if oid in open_ids:
                continue
            try:
                st = await self.c.order_state(oid)
            except RpcError as e:
                if getattr(e, "code", None) == 10004 and self.store.order_filled_in_ledger(oid) < 1e-9:
                    # Deribit ลบออเดอร์ที่ถูกยกเลิกโดยไม่มี fill ออกจาก get_order_state → order_not_found
                    # ไม่มี fill ในบัญชีไม้ (ดึง trade ล่าสุดแล้วก่อนถึงตรงนี้) = ยกเลิกไปแล้วแน่นอน ปลดล็อกชั้นนี้
                    log.info("ออเดอร์ %s ชั้น %s ไม่อยู่ในระบบ Deribit แล้ว และไม่มี fill → ถือว่ายกเลิกแล้ว", oid, k)
                    del known[oid]
                    self.pending.pop((k, side), None)
                    continue
                log.warning("order_state %s: %s", oid, e)
                unresolved.add(k)
                continue
            state, filled = st.get("order_state"), float(st.get("filled_amount") or 0)
            if state in ("filled", "cancelled", "rejected") and abs(self.store.order_filled_in_ledger(oid) - filled) < 1e-6:
                del known[oid]                              # (รายการรับช่วง adopted เก็บไว้ถาวร — order id ไม่ซ้ำ)
                self.pending.pop((k, side), None)
            else:
                unresolved.add(k)
        return unresolved

    # ---------- วงรอบหลัก ----------
    async def reconcile(self):
        st = self.store.data["status"]
        if st in ("halted", "closed"):
            return
        r = await self.stable_read()
        if r is None:
            return await self._flux("สถานะเปลี่ยนระหว่างอ่าน 2 รอบ")
        new, foreign = await self.sync_ledger()
        for t, k in new:
            await self._notify_fill(t, k, r)
        if foreign:
            if abs(r["position"]) < 1e-9 and self.store.data["seen_full"]:
                return await self.close_zone("พบการปิด position นอกบอทหลัง zone เต็ม — ถือว่าต่อพอร์ตแล้ว")
            self.store.data["foreign_trade_ids"] = self.store.data.get("foreign_trade_ids", []) + [t["trade_id"] for t in foreign]
            return await self.halt(f"มี fill ที่ไม่ใช่ของบอท {len(foreign)} รายการ (เทรดมือ หรือโดน liquidate?)")
        unresolved = await self.resolve_missing(r["orders"])
        now = self.now()
        for (k, side), t0 in list(self.pending.items()):
            has = any(self.level_of(o.label, o.order_id) == k and o.side == side for o in r["orders"])
            if has or now - t0 > self.sf.pending_ttl:
                self.pending.pop((k, side), None)
        snap = Snapshot(position=r["position"], mark=r["mark"], best_bid=r["bid"], best_ask=r["ask"], orders=r["orders"],
                        ledger=self.store.nets(), unresolved=unresolved, pending=set(self.pending), seen_full=self.store.data["seen_full"],
                        order_fills={o.order_id: self.store.order_filled_in_ledger(o.order_id) for o in r["orders"]},
                        last_side=self.store.last_side(), adopted=dict(self.store.data.get("adopted", {})))
        d = plan(self.cfg, snap)
        self.last_snap, self.last_decision = snap, d
        if d.status == "flux":
            return await self._flux(d.reason)
        if d.status == "halt":
            return await self.halt(d.reason)
        self.flux_since = None
        if self.cfg.held(r["position"]) >= self.cfg.cap - 1e-9 and not self.store.data["seen_full"]:
            self.store.data["seen_full"] = True
            w = self.cfg.word
            self.n.post(f"🟧 zone เต็ม {self.cfg.n}/{self.cfg.n} ชั้น ({w} {self.cfg.cap:,} USD) — ถึงเวลาต่อพอร์ต · บอทหยุดเพิ่ม {w} คงไว้แต่ {self.cfg.close_side}")
        own_n = sum(1 for o in r["orders"] if self.level_of(o.label, o.order_id) is not None)
        want_n = d.info.get("want_orders", self.cfg.n)
        self.store.data["heartbeat"] = {"ts": self.now(), "position": r["position"], "mark": r["mark"], "own_orders": own_n, "want_orders": want_n,
                                        "exposure": d.info.get("exposure"), "levels_short": d.info.get("levels_short"),
                                        "actions": len(d.actions), "live": self.live}
        if self.now() - self.last_hb_log >= self.sf.heartbeat_sec:
            self.last_hb_log = self.now()
            log.info("%s", f"✓ ทำงานปกติ · ราคา {r['mark']:,.1f} · position {r['position']:+,.0f} "
                           f"({len(d.info.get('levels_short') or [])} ชั้น) · ออเดอร์ของบอท {own_n}/{want_n} · "
                           f"ความเสี่ยงรวม {d.info.get('exposure', 0):,.0f}/{self.cfg.cap:,}")
        await self.execute(d.actions)
        if self.live:
            self.store.save()
        await self._check_liq(r)                                  # แค่เตือน — ทำหลังยิงออเดอร์ (ไม่ถ่วงเวลาไม้ฝั่งตรงข้าม)

    async def _flux(self, reason):
        now = self.now()
        if self.flux_since is None:
            self.flux_since = now
        log.info("รอข้อมูลนิ่ง: %s", reason)
        if now - self.flux_since > self.sf.flux_halt_sec:
            return await self.halt(f"ข้อมูลไม่นิ่งเกิน {self.sf.flux_halt_sec:.0f} วิ: {reason}")
        self.wake.set()                                       # ลองใหม่เร็ว ๆ

    @staticmethod
    def is_throttle(e) -> bool:
        """rate limit / เครื่องจับคู่ออเดอร์ยุ่ง ของ Deribit (10028 too_many_requests, 10040 retry, 13888 timed_out)"""
        m = str(e).lower()
        return getattr(e, "code", None) in (10028, 10040, 10041, 13888) or "too_many" in m or "retry" in m

    def prioritize(self, actions, mark):
        """ยกเลิกก่อน → market → limit ที่ใกล้ราคาที่สุด (ออเดอร์ฝั่งตรงข้ามหลัง fill เร่งด่วนที่สุด)"""
        def key(a):
            if a[0] == "cancel":
                return (0, 0)
            if a[0] == "market":
                return (1, 0)
            return (2, abs(a[3] - mark) if mark else 0)
        return sorted(actions, key=key)

    async def execute(self, actions):
        if not actions:
            return
        mark = self.last_snap.mark if self.last_snap else None
        actions = self.prioritize(actions, mark)
        if not self.live:
            sig = tuple(a[:5] for a in actions)
            if sig != self.last_dry:
                self.last_dry = sig
                for a in actions:
                    log.info("[DRY-RUN] %s", self.describe(a))
            return
        if self.now() < self.throttle_until:
            log.info("พักส่งคำสั่ง (rate limit) อีก %.1f วิ", self.throttle_until - self.now())
            self.wake.set()
            return
        sent = 0
        for a in actions:
            if a[0] != "cancel" and sent >= self.sf.max_orders_per_cycle:
                log.info("ส่งครบโควตารอบนี้ %d คำสั่ง ที่เหลือรอบหน้า", sent)
                break
            if sent:
                await self.sleep(self.sf.order_spacing_sec)
            sent += 1
            try:
                if a[0] == "cancel":
                    await self.c.cancel(a[1])
                    log.info("ยกเลิก %s", self.describe(a))
                    continue
                if a[0] != "limit":
                    raise RuntimeError(f"ปฏิเสธคำสั่งที่ไม่ใช่ post-only: {a}")      # กันพลาด: บอทนี้ไม่ส่ง market
                _, k, side = a[:3]
                price = a[3] if a[0] == "limit" else None
                amount, ro = (a[4], a[5]) if a[0] == "limit" else (a[3], a[4])
                self.pending[(k, side)] = self.now()
                res = await self.c.place(side, self.cfg.instrument, amount, price=price, label=self.cfg.label(k), reduce_only=ro)
                oid = res["order"]["order_id"]
                self.store.data["known_orders"][oid] = [k, side, amount]
                log.info("%s", f"วาง {self.describe(a)} → {oid}{self._latency(k, side, res['order'])}")
                if a[0] == "market":
                    self.n.post(f"⚡ {self.describe(a)}")
                self.throttle_sec = 0.0
            except RpcTimeout as e:
                self.n.post(f"⚠️ ส่งคำสั่งแล้วไม่ได้คำตอบ ({e}) — ล็อกชั้นไว้ {self.sf.pending_ttl:.0f} วิ แล้วตรวจใหม่", key="timeout")
            except RpcError as e:
                if a[0] != "cancel":
                    self.pending.pop((a[1], a[2]), None)
                if self.is_throttle(e):
                    self.throttle_sec = min(max(self.throttle_sec * 2, 1.0), self.sf.throttle_max_sec)
                    self.throttle_until = self.now() + self.throttle_sec
                    self.n.post(f"🐢 Deribit จำกัดความถี่ ({e}) — พัก {self.throttle_sec:.0f} วิ แล้วส่งต่อ", key="throttle")
                    break
                msg = str(e)
                key = "margin" if "funds" in msg or "margin" in msg else f"rej{a[1]}"
                self.n.post(f"⚠️ {self.describe(a)} ไม่ผ่าน: {msg}", key=key)
        self.wake.set()

    def _latency(self, k, side, order) -> str:
        """ไม้ฝั่งตรงข้ามหลัง fill: เวลาจาก fill จนออเดอร์ active บน exchange (creation_timestamp ของ Deribit)"""
        want = "sell" if side == "buy" else "buy"
        f = next((f for f in reversed(self.store.data.get("fills") or []) if f["k"] == k and f["side"] == want), None)
        if not f:
            return ""
        t1 = order.get("creation_timestamp") or self.now() * 1000
        dt = (t1 - f["ts"]) / 1000
        return f" · fill→active {dt:.2f} วิ" if 0 <= dt < 600 else ""

    def status_text(self) -> str:
        d = self.store.data
        hb = d.get("heartbeat") or {}
        age = self.now() - hb["ts"] if hb.get("ts") else None
        st = d.get("status")
        head = {"active": "✅ ทำงานอยู่", "halted": "🛑 หยุด (halt)", "closed": "✅ zone ปิดแล้ว", "new": "⏳ ยังไม่รับช่วง"}.get(st, st)
        lines = [f"{head} · {'LIVE' if self.live else 'DRY-RUN'} · zone {self.cfg.word.upper()} {self.cfg.lower:,.0f}–{self.cfg.upper:,.0f} gap {self.cfg.gap:,.0f} × {self.cfg.size:,}"]
        if st == "halted":
            lines.append(f"เหตุผล: {d.get('halt_reason')}")
        if hb:
            lines.append(f"ตรวจล่าสุด {age:,.0f} วิที่แล้ว · ราคา {hb['mark']:,.1f}")
            lines.append(f"position {hb['position']:+,.0f} · ชั้นที่ {self.cfg.word} {len(hb.get('levels_short') or [])}/{self.cfg.n} · ออเดอร์ของบอท {hb['own_orders']}/{hb.get('want_orders', self.cfg.n)}")
            lines.append(f"ความเสี่ยงรวม {hb.get('exposure') or 0:,.0f}/{self.cfg.cap:,} USD")
        fills = d.get("fills") or []
        if fills:
            f = fills[-1]
            lines.append(f"fill ล่าสุด: {f['side'].upper()} {f['price']:,.1f} × {f['amount']:,.0f} ชั้น {f['k']}")
        lines.append(f"รันมา {(self.now() - self.started_at) / 3600:,.1f} ชม. · fill ทั้งหมด {len(fills)} ครั้ง")
        return "\n".join(lines)

    async def tg_loop(self):
        """รับคำสั่งจาก Telegram (/status รวมทุกบอท — ดึงข้อความตัวเดียวผ่าน lock) + ส่งสรุปตามรอบ · ดู tghub.py"""
        await tg_loop(self)

    def describe(self, a):
        if a[0] == "cancel":
            return f"cancel {a[1]} ชั้น {a[2]} ({a[3]})"
        if a[0] == "limit":
            return f"{a[2].upper()} limit {a[3]:,.1f} × {a[4]:,.0f} ชั้น {a[1]}{' reduce-only' if a[5] else ''}"
        return f"{a[2].upper()} MARKET × {a[3]:,.0f} ชั้น {a[1]}{' reduce-only' if a[4] else ''} — {a[5]}"

    async def _notify_fill(self, t, k, r):
        amt, px = float(t["amount"]), float(t["price"])
        line = f"{'🔴 SELL' if t['direction'] == 'sell' else '🟢 BUY'} {px:,.1f} × {amt:,.0f} (ชั้น {k}/{self.cfg.n}) · pos {r['position']:+,.0f}"
        opn = self.cfg.open_side
        if t["direction"] == self.cfg.close_side:                 # ไม้ปิด → คิดกำไรคู่กับไม้เปิดล่าสุดของชั้นนี้
            op = self.store.data["ledger"].get(f"last_{opn}_px", {}).get(str(k))
            if op:
                # inverse: short เปิด sell op ปิด buy px → q(1/px − 1/op) · long เปิด buy op ปิด sell px → q(1/op − 1/px)
                gross = amt * (1 / float(op) - 1 / px) if self.cfg.is_long else amt * (1 / px - 1 / float(op))
                # fee จริงที่ Deribit เก็บ (BTC, บวก = จ่าย · ลบ = rebate) · ไม้เปิดที่ไม่รู้ fee (เช่น fill ก่อนบอทรับช่วง) ใช้อัตราเดียวกับไม้ปิด
                fee_c = float(t.get("fee") or 0)
                rate = self.store.data["ledger"].get(f"last_{opn}_fee", {}).get(str(k))
                fee_o = float(rate) * amt if rate is not None else fee_c
                net = gross - fee_c - fee_o
                line += (f" · คู่นี้ {gross:+.7f} BTC · สุทธิหลัง fee {net:+.7f} BTC (~${net * px:,.2f})"
                         + ("" if rate is not None else f" (fee {'ซื้อ≈ขาขาย' if self.cfg.is_long else 'ขาย≈ขาซื้อ'})"))
        self.store.log_fill({"ts": t["timestamp"], "k": k, "side": t["direction"], "price": px, "amount": amt, "trade_id": t["trade_id"]})
        self.n.post(line)

    def projected_liq(self, r, acc, nets: dict):
        """liq ถ้าราคาวิ่งจน zone เต็ม (ออเดอร์เปิดที่เหลือ fill ครบ) ด้วยเงินในบัญชีตอนนี้ · short: ราคาขึ้น · long: ราคาลง"""
        pos = r["pos"]
        S = max(0.0, self.cfg.held(r["position"]))
        mark = r["mark"]
        r_ = mm_rate(float(pos.get("maintenance_margin") or 0), mark, S, self.sf.default_mmr)
        eq = eq_eff(float(acc.get("equity") or 0), float(acc.get("maintenance_margin") or 0), float(pos.get("maintenance_margin") or 0))
        adds = [(self.cfg.open_price(k), self.cfg.size - nets.get(k, 0.0)) for k in range(1, self.cfg.n + 1) if self.cfg.size - nets.get(k, 0.0) > 1e-9]
        f = liq_long_after_fills if self.cfg.is_long else liq_short_after_fills
        return f(S, float(pos.get("average_price") or mark), mark, eq, r_, adds), r_

    def liq_too_close(self, liq) -> tuple[bool, float, str]:
        """(ใกล้เกิน, เส้นที่ต้องผ่าน, คำอธิบาย) · short: liq ต้อง ≥ upper + buffer · long: liq ต้อง ≤ lower − buffer"""
        if self.cfg.is_long:
            need = self.cfg.lower - self.sf.liq_buffer
            return liq is not None and liq > need, need, f"สูงกว่า lower − buffer ({need:,.0f})"
        need = self.cfg.upper + self.sf.liq_buffer
        return liq is not None and liq < need, need, f"ต่ำกว่า upper + buffer ({need:,.0f})"

    async def _check_liq(self, r):
        try:
            acc = await self.c.account_summary("BTC")
        except Exception as e:
            log.warning("account_summary: %s", e)
            return
        liq, _ = self.projected_liq(r, acc, self.store.nets())
        bad, _, why = self.liq_too_close(liq)
        if bad:
            self.n.post(f"🟥 liq ถ้า zone เต็ม ≈ {liq:,.0f} {why} — เติมเงินหรือลดขนาด", key="liq")

    # ---------- เหตุการณ์พิเศษ ----------
    async def halt(self, reason):
        self.store.data.update(status="halted", halt_reason=reason)
        cancelled = 0
        if self.live:
            try:
                for o in await self.c.open_orders(self.cfg.instrument):
                    if self.level_of(o.get("label"), o["order_id"]) is not None and o["direction"] == self.cfg.open_side:
                        await self.c.cancel(o["order_id"])
                        cancelled += 1
            except Exception as e:
                log.error("ยกเลิก sell ตอน halt ไม่สำเร็จ: %s", e)
            self.store.save()
        log.error("HALT: %s", reason)
        await self.n.flush()
        await self.n.send(f"🛑 HALT: {reason}\nยกเลิก {self.cfg.open_side} ของบอท {cancelled} ตัว · {self.cfg.close_side} reduce-only ยังอยู่ · ตรวจแล้วรันใหม่ด้วย --clear-halt")

    async def close_zone(self, reason):
        n = 0
        if self.live:
            for o in await self.c.open_orders(self.cfg.instrument):
                if self.level_of(o.get("label"), o["order_id"]) is not None:
                    await self.c.cancel(o["order_id"])
                    n += 1
            self.store.data["status"] = "closed"
            self.store.save()
        else:
            self.store.data["status"] = "closed"
        await self.n.flush()
        await self.n.send(f"✅ ปิด zone {self.cfg.zone_id}: {reason} · ยกเลิกออเดอร์ {n} ตัว · zone นี้จะไม่ถูกเปิดซ้ำ")

    async def adopt(self):
        """รับช่วงครั้งแรก: อ่าน position → ตั้ง baseline ชั้น 1..m0 → ออเดอร์มือที่ตรงสูตร (ฝั่ง/ราคา/ขนาด, ยังไม่ fill)
        นับเป็นของบอทเลยโดยผูก order id กับชั้น · ชั้นละตัวเดียว · ตัวที่ไม่ตรงสูตรยกเลิก · ชั้นที่ขาดบอทวางเพิ่มเอง"""
        r = await self.stable_read()
        if r is None:
            raise Halt("สถานะไม่นิ่งตอนรับช่วง ลองใหม่")
        S = self.cfg.held(r["position"])
        size = self.cfg.size
        if S < 0 or abs(S / size - round(S / size)) > 1e-9:
            raise Halt(f"position {r['position']:+,.0f} ไม่ใช่ {self.cfg.word} ทวีคูณของ {size}")
        m0 = int(round(S / size))
        lo, hi = m_range(self.cfg, r["mark"])
        if not (lo <= m0 <= hi and m0 <= self.cfg.n):
            raise Halt(f"position {m0} ชั้นไม่สอดคล้องกับราคา {r['mark']:,.1f} (ควร {lo}–{hi} ชั้น) — ตรวจกรอบ/ขนาด")
        exp = expected(self.cfg, m0)
        foreign = [o for o in r["orders"] if parse_label(o.label, self.cfg.zone_id)[0] != "own"]
        adopted, drop = {}, []
        taken = set()
        for o in sorted(foreign, key=lambda o: o.order_id):
            hit = next((k for k, (side, px) in exp.items()
                        if k not in taken and o.side == side and abs(o.price - px) < 1e-6 and abs(o.amount - size) < 1e-9 and o.filled == 0), None)
            if hit is None:
                drop.append(o)
            else:
                taken.add(hit)
                adopted[o.order_id] = hit
        if foreign and not self.sf.adopt_manual:
            raise Halt(f"มีออเดอร์มือ {len(foreign)} ตัว (ตรงสูตร {len(adopted)} · ไม่ตรง {len(drop)}) — รันด้วย --adopt-manual "
                       "เพื่อรับตัวที่ตรงสูตรเป็นของบอทและยกเลิกตัวที่ไม่ตรง")
        for o in drop:
            log.info("%sยกเลิกออเดอร์มือที่ไม่ตรงสูตร %s %s %.1f × %.0f (fill %.0f)", "" if self.live else "[DRY-RUN] ",
                     o.order_id, o.side, o.price, o.amount, o.filled)
            if self.live:
                await self.c.cancel(o.order_id)
            else:
                self.dry_ignore.add(o.order_id)
        for oid, k in sorted(adopted.items(), key=lambda x: x[1]):
            side, px = exp[k]
            log.info("รับออเดอร์มือ %s เป็นชั้น %d (%s %.1f)", oid, k, side, px)
        d = self.store.data
        d["baseline"] = {str(k): float(size) for k in range(1, m0 + 1)}
        d["adopted"] = adopted
        d["known_orders"].update({oid: [k, exp[k][0], float(size)] for oid, k in adopted.items()})
        d["ledger_start_ms"] = int(self.now() * 1000)
        d["adopted_at"] = d["ledger_start_ms"]
        d["status"] = "active"
        d["seen_full"] = m0 == self.cfg.n
        if self.live:
            self.store.save()
        missing = self.cfg.n - len(adopted)
        await self.n.send(f"รับช่วง zone: position {r['position']:+,.0f} = {m0}/{self.cfg.n} ชั้น · รับออเดอร์มือเป็นของบอท {len(adopted)} ตัว"
                          f" · ยกเลิกตัวไม่ตรงสูตร {len(drop)} · บอทจะวางเพิ่ม {missing} ตัว")
        return r, m0

    async def catch_up(self, r):
        """เริ่มบอทใหม่บน zone ที่ active แล้ว (ก่อน preflight):
        1) ดึง fill ที่เกิดตอนบอทหยุดเข้าบัญชีไม้ก่อน — ไม่งั้น preflight นับชั้นที่เพิ่ง fill เป็น sell ที่ยังรออยู่ซ้ำ → liq ต่ำกว่าจริง
        2) --adopt-manual: ออเดอร์มือที่ตรงสูตร ณ บัญชีไม้ตอนนี้ (ฝั่ง/ราคา/ขนาด, ยังไม่ fill, ชั้น-ฝั่งนั้นยังไม่มีออเดอร์ของบอท)
           นับเป็นของบอทเหมือนตอนรับช่วง — เช่น ผู้ใช้วาง buy คืนเองตอนบอทหยุด"""
        new, _ = await self.sync_ledger()                    # fill แปลกปลอมปล่อยให้ reconcile ตัดสิน (halt) ตามเดิม
        for t, k in new:
            await self._notify_fill(t, k, r)
        got = []
        if self.sf.adopt_manual:
            size, nets = self.cfg.size, self.store.nets()
            adopted = self.store.data.setdefault("adopted", {})
            have = {(self.level_of(o.label, o.order_id), o.side) for o in r["orders"]}
            for o in sorted(r["orders"], key=lambda o: o.order_id):
                if parse_label(o.label, self.cfg.zone_id)[0] != "foreign" or o.order_id in adopted:
                    continue
                for k in range(1, self.cfg.n + 1):
                    net = nets.get(k, 0.0)
                    side, px = ((self.cfg.close_side, self.cfg.close_price(k)) if abs(net - size) < 1e-9 else
                                (self.cfg.open_side, self.cfg.open_price(k)) if abs(net) < 1e-9 else (None, None))
                    if (o.side == side and abs(o.price - px) < 1e-6 and abs(o.amount - size) < 1e-9 and o.filled == 0
                            and (k, side) not in have):
                        adopted[o.order_id] = k
                        self.store.data["known_orders"][o.order_id] = [k, side, float(size)]
                        have.add((k, side))
                        got.append((k, side, px))
                        log.info("รับออเดอร์มือ %s เป็นชั้น %d (%s %.1f)", o.order_id, k, side, px)
                        break
        if self.live:
            self.store.save()
        if new or got:
            await self.n.send(f"เริ่มใหม่: fill ระหว่างบอทหยุด {len(new)} รายการ · รับออเดอร์มือที่ตรงสูตรเป็นของบอท {len(got)} ตัว"
                              + "".join(f"\n  ชั้น {k} {side.upper()} {px:,.1f}" for k, side, px in got))
        return new, got

    async def preflight(self, r):
        acc = await self.c.account_summary("BTC")
        liq, r_ = self.projected_liq(r, acc, self.store.nets())
        bad, _, why = self.liq_too_close(liq)
        msg = (f"equity {float(acc.get('equity') or 0):.6f} BTC · available {float(acc.get('available_funds') or 0):.6f} · "
               f"MM rate {r_ * 100:.3f}% · liq ถ้า zone เต็ม ≈ {liq:,.0f}" if liq else "ไม่มีจุด liq")
        log.info("preflight: %s", msg)
        if bad and not self.sf.force:
            raise Halt(f"liq ถ้า zone เต็ม ≈ {liq:,.0f} {why} — เติมเงิน/ลดขนาด หรือใช้ --force")
        return msg

    async def run(self):
        ins = self.cfg.instrument
        backoff = 2
        started = False
        while True:
            try:
                self.c.on_event = lambda p: self.wake.set()
                await self.c.connect()
                await self.c.subscribe([f"user.changes.{ins}.raw"])
                if self.store.data["status"] == "new":
                    r, m0 = await self.adopt()
                else:
                    r = await self.read()
                    if not started:
                        await self.catch_up(r)
                if not started:
                    pf = await self.preflight(r)
                    asyncio.create_task(self.tg_loop())
                    await self.n.send(f"▶️ เริ่มบอท {'LIVE' if self.live else 'DRY-RUN'} · {self.cfg.instrument} {self.cfg.word.upper()} {self.cfg.lower:,.0f}–{self.cfg.upper:,.0f} "
                                      f"gap {self.cfg.gap:,.0f} × {self.cfg.size:,} ({self.cfg.n} ชั้น)\n{pf}\nพิมพ์ /status เพื่อดูสถานะ")
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
                        await self.sleep(self.sf.debounce_sec)       # รวบ event ที่มาติด ๆ กัน
                    except asyncio.TimeoutError:
                        pass
            except Halt as e:
                await self.n.flush()
                await self.n.send(f"🛑 เริ่มไม่ได้: {e}")
                await self.c.close()
                return "refused"
            except Exception as e:
                # สายหลุด / timeout / Deribit ตอบ error (เช่น too_many_requests ตอนอ่าน) / error ไม่คาดคิด
                # เดิม RpcError ตอน "อ่าน" ไม่ถูกดัก → process ตายเงียบ ๆ · ตอนนี้: ปิดสาย พักแบบทวีคูณ แล้วสร้างสถานะจาก exchange ใหม่
                if not started and isinstance(e, RpcError) and not self.is_throttle(e):
                    await self.n.flush()                      # ยังไม่เคยเริ่มได้ + error ถาวร (เช่น key ผิด) → ไม่วนลองไปเรื่อย ๆ
                    await self.n.send(f"🛑 เริ่มไม่ได้: {e}")
                    await self.c.close()
                    return "refused"
                if isinstance(e, (ConnectionError, OSError, RpcTimeout)):
                    msg, key = f"🔌 สายหลุด/timeout: {e}", "disconnect"
                elif isinstance(e, RpcError):
                    msg, key = f"{'🐢 Deribit จำกัดความถี่' if self.is_throttle(e) else '⚠️ Deribit ตอบ error'}: {e}", "rpcerr"
                else:
                    log.exception("error ไม่คาดคิด")
                    msg, key = f"⚠️ error ไม่คาดคิด {type(e).__name__}: {e}", "unexpected"
                log.warning("%s", f"{msg} — ต่อใหม่ใน {backoff} วิ")
                self.n.post(f"{msg} — ต่อใหม่ใน {backoff} วิ", key=key)
                try:
                    await self.c.close()
                except Exception:
                    pass
                await self.sleep(backoff)
                backoff = min(backoff * 2, 60)
