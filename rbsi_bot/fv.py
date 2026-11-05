# -*- coding: utf-8 -*-
"""Fixed Asset Value — ตรรกะล้วน (ไม่แตะเครือข่าย ทดสอบได้ทั้งหมด)

ถือ BTC ให้มีมูลค่าคงที่ V (USD) · มูลค่าห่างจาก V ถึง Δ → ขาย/ซื้อส่วนต่างกลับเข้า V ด้วย post-only
ราคา trigger คำนวณจากจำนวน BTC ที่ถืออย่างเดียว (ไม่ต้องจำราคาเริ่ม):
  ขายเมื่อ qty·P ≥ V+Δ  → ราคาขาย Ps = (V+Δ)/qty · ขาย qty − V/Ps  (= Δ USD พอดี)
  ซื้อเมื่อ qty·P ≤ V−Δ  → ราคาซื้อ Pb = (V−Δ)/qty · ซื้อ V/Pb − qty  (= Δ USD พอดี)
ราคาตลาดเลย trigger ไปแล้ว (ตอนเริ่ม / ราคากระโดด / fill แล้วราคาวิ่งต่อ) → วาง post-only ที่ราคา maker ดีที่สุด
(bid+tick สำหรับขาย · ask−tick สำหรับซื้อ) แล้วปรับให้ครบในไม้เดียว (ขนาดคิดจากราคาที่วางจริง)

ออเดอร์ที่มีอยู่ "ถูกต้อง" เมื่อ fill ส่วนที่เหลือครบแล้วมูลค่า ณ ราคาของมันกลับมาเท่า V พอดี (ใช้ได้ทั้งตอน fill บางส่วน)
ไล่ราคา: ถ้าราคาตลาดยังเลย trigger อยู่ แต่ไม้ของเราห่างราคา maker ดีที่สุดเกิน chase_bps (ตลาดขยับหนีหลังวาง)
→ ยกเลิกแล้ววางใหม่ที่ราคาดีที่สุด (รอบถัดไป หลังยืนยันว่ายกเลิกสำเร็จ)"""
from __future__ import annotations
import math
from dataclasses import dataclass, field

from .grid import Order, fmtnum

EPS = 1e-12


def floor_to(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 10)


def ceil_to(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-9) * step, 10)


@dataclass(frozen=True)
class FVConfig:
    value: float                   # V: มูลค่า BTC ที่ต้องการถือ (USD)
    mode: str = "usd"              # "usd" = step เป็น USD · "pct" = step เป็น % ของ V
    step: float = 100.0            # Δ (usd) หรือ % (pct เช่น 3 = 3%)
    instrument: str = "BTC_USDC"
    tick: float = 0.01
    lot: float = 1e-8
    min_order_usd: float = 5.0     # ไม้เล็กกว่านี้ไม่วาง (เศษจากการปัด)
    max_order_usd: float = 0.0     # 0 = ไม่จำกัด · ราคากระโดดแรงจะแบ่งเป็นหลายไม้
    fee_rate: float = 0.0002       # maker ของ Deribit spot — กันเงินให้พอจ่าย fee ตอนซื้อ
    chase_bps: float = 5.0         # ไม้ไล่ราคา (ราคาเลย trigger แล้ว) ห่างราคา maker ดีที่สุดเกินนี้ (0.05%) → วางใหม่

    def __post_init__(self):
        errs = []
        if not self.value > 0:
            errs.append("value (Fix Value) ต้องมากกว่า 0")
        if self.mode not in ("usd", "pct"):
            errs.append('mode ต้องเป็น "usd" หรือ "pct"')
        elif self.mode == "usd" and not 0 < self.step < self.value / 2:
            errs.append("step (USD) ต้องอยู่ระหว่าง 0 ถึงครึ่งหนึ่งของ value")
        elif self.mode == "pct" and not 0 < self.step < 50:
            errs.append("step (%) ต้องอยู่ระหว่าง 0–50")
        if "_" not in self.instrument:
            errs.append("instrument ต้องเป็นคู่ spot เช่น BTC_USDC")
        if self.min_order_usd < 0 or (self.max_order_usd and self.max_order_usd < max(self.min_order_usd, 1)):
            errs.append("max_order_usd ต้องเป็น 0 (ไม่จำกัด) หรือมากกว่า min_order_usd")
        if errs:
            raise ValueError("; ".join(errs))

    @property
    def delta(self) -> float:
        return self.step if self.mode == "usd" else self.value * self.step / 100

    @property
    def base(self) -> str:
        return self.instrument.split("_")[0]

    @property
    def quote(self) -> str:
        return self.instrument.split("_")[1]

    @property
    def fv_id(self) -> str:
        s = f"{self.base}{self.quote}-V{fmtnum(self.value)}-" + (f"D{fmtnum(self.step)}" if self.mode == "usd" else f"P{fmtnum(self.step)}")
        return s.replace(".", "p")

    def label(self, side: str) -> str:
        return f"fv_{self.fv_id}_{'s' if side == 'sell' else 'b'}"


def triggers(cfg: FVConfig, qty: float):
    """(ราคาขาย, ราคาซื้อ) ที่มูลค่าแตะ V±Δ · ไม่มี BTC = ไม่มีราคาขาย และซื้อได้ทันที"""
    if qty <= EPS:
        return None, math.inf
    return (cfg.value + cfg.delta) / qty, (cfg.value - cfg.delta) / qty


def order_amount(cfg: FVConfig, side: str, qty: float, price: float) -> float:
    """จำนวน BTC ที่ขาย/ซื้อ ณ price แล้วมูลค่ากลับมาเท่า V พอดี (ปัดลงตาม lot · จำกัดด้วย max_order_usd)"""
    raw = qty - cfg.value / price if side == "sell" else cfg.value / price - qty
    amt = floor_to(max(raw, 0.0), cfg.lot)
    if cfg.max_order_usd:
        amt = min(amt, floor_to(cfg.max_order_usd / price, cfg.lot))
    return amt


def desired(cfg: FVConfig, side: str, qty: float, bid: float, ask: float):
    """(ราคา, จำนวน) ของออเดอร์ที่ควรวาง หรือ None"""
    ps, pb = triggers(cfg, qty)
    if side == "sell":
        if ps is None:
            return None
        price = ceil_to(max(ps, bid + cfg.tick), cfg.tick)          # post-only: ต้องสูงกว่า bid
    else:
        price = floor_to(min(pb, ask - cfg.tick), cfg.tick)         # post-only: ต้องต่ำกว่า ask
        if price <= 0:
            return None
    amt = order_amount(cfg, side, qty, price)
    if amt <= 0 or amt * price < cfg.min_order_usd:
        return None
    return price, amt


def consistent(cfg: FVConfig, side: str, qty: float, price: float, remaining: float) -> bool:
    """ออเดอร์นี้ fill ส่วนที่เหลือครบแล้วมูลค่ากลับเข้า V ณ ราคาของมันไหม (ยอมคลาดเคลื่อนจากการปัด)"""
    want = order_amount(cfg, side, qty, price)
    return want > 0 and abs(remaining - want) <= max(3 * cfg.lot, 0.002 * want)


@dataclass
class FVDecision:
    actions: list = field(default_factory=list)      # ("cancel", oid, side, เหตุผล) · ("place", side, price, amount)
    info: dict = field(default_factory=dict)


def plan(cfg: FVConfig, qty: float, cash: float, bid: float, ask: float, own: list, pending=(), band=None) -> FVDecision:
    """own = list[Order] ของบอทบนคู่นี้ · pending = ฝั่งที่เพิ่งส่งคำสั่ง (ห้ามส่งซ้ำ)
    band = (min_price, max_price) ของ Deribit (~±2% จาก index) — ออเดอร์นอกกรอบ Deribit ยกเลิกทิ้ง → ยังไม่วาง รอราคาเข้ามาใกล้
    (กรอบขยับตาม index: ราคาเข้าใกล้ trigger ~2% ออเดอร์ก็อยู่ในกรอบแล้ว ทันก่อนราคาถึง trigger)
    กติกากันซ้ำ: ต่อฝั่งมีออเดอร์ได้ตัวเดียว · ฝั่งที่เพิ่งสั่งยกเลิก จะวางใหม่รอบถัดไปหลังยืนยันว่าหายไปแล้ว"""
    d = FVDecision()
    mid = (bid + ask) / 2
    ps, pb = triggers(cfg, qty)
    d.info.update(mid=mid, value=qty * mid, dev=qty * mid - cfg.value, sell_at=ps, buy_at=None if pb == math.inf else pb, usdc_short=None, waiting_band={})
    for side in ("sell", "buy"):
        keep = None
        cancelled = False
        for o in sorted((o for o in own if o.side == side), key=lambda o: o.order_id):
            if keep is None and consistent(cfg, side, qty, o.price, o.remaining):
                keep = o
                best = bid + cfg.tick if side == "sell" else ask - cfg.tick
                crossed = (ps is not None and best >= ps) if side == "sell" else (best <= pb)
                behind = (o.price - best) if side == "sell" else (best - o.price)
                if crossed and behind > best * cfg.chase_bps / 1e4:
                    d.actions.append(("cancel", o.order_id, side, "ไล่ราคา"))
                    cancelled = True
            else:
                d.actions.append(("cancel", o.order_id, side, "ซ้ำ" if keep is not None else "ไม่ตรงยอดปัจจุบัน"))
                cancelled = True
        if keep is not None or cancelled or side in pending:
            continue
        want = desired(cfg, side, qty, bid, ask)
        if want is None:
            continue
        price, amt = want
        if band and not (band[0] - 1e-9 <= price <= band[1] + 1e-9):
            d.info["waiting_band"][side] = price                     # นอกกรอบราคาของ Deribit → รอ
            continue
        if side == "buy":
            cost = amt * price * (1 + cfg.fee_rate)
            if cost > cash + 1e-9:
                d.info["usdc_short"] = cost                          # USDC ไม่พอ → หยุดฝั่งซื้อ (ฝั่งขายทำงานต่อ)
                continue
        elif amt > qty + EPS:
            continue
        d.actions.append(("place", side, price, amt))
    return d


def ladder(cfg: FVConfig, qty: float, cash: float, levels: int = 5):
    """ภาพบันไดถ้าราคาวิ่งทางเดียวทีละขั้น (ใช้แสดงผล/วางแผน): list ของ (side, price, amount, qty หลัง, cash หลัง)"""
    out = []
    for side in ("sell", "buy"):
        q, c = qty, cash
        for _ in range(levels):
            ps, pb = triggers(cfg, q)
            if (ps is None) if side == "sell" else (pb == math.inf):
                break
            price = ceil_to(ps, cfg.tick) if side == "sell" else floor_to(pb, cfg.tick)
            if price <= 0:
                break
            amt = order_amount(cfg, side, q, price)
            if amt <= 0:
                break
            if side == "buy" and amt * price * (1 + cfg.fee_rate) > c + 1e-9:
                out.append((side, price, amt, q, c, "USDC ไม่พอ"))
                break
            q, c = (q - amt, c + amt * price * (1 - cfg.fee_rate)) if side == "sell" else (q + amt, c - amt * price * (1 + cfg.fee_rate))
            out.append((side, price, amt, q, c, ""))
    return out


def cash_runs_out(cfg: FVConfig, qty: float, cash: float) -> float | None:
    """ราคาที่ USDC หมด ถ้าราคาลงทางเดียว (จำลองทีละขั้นจริง รวม fee) · None = ไม่หมดก่อนราคาเหลือ 1% ของปัจจุบัน"""
    q, c = qty, cash
    if q <= EPS:
        return None
    start = cfg.value / q
    for _ in range(100_000):
        _, pb = triggers(cfg, q)
        price = floor_to(pb, cfg.tick)
        if price < start * 0.01:
            return None
        amt = order_amount(cfg, "buy", q, price)
        if amt <= 0:
            return None
        cost = amt * price * (1 + cfg.fee_rate)
        if cost > c:
            return price
        q, c = q + amt, c - cost
    return None
