# -*- coding: utf-8 -*-
"""KZM Grid (Dynamic Ratio) บน spot — ตรรกะล้วน (ไม่แตะเครือข่าย ทดสอบได้ทั้งหมด)

กรอบ [floor, ceil] แบ่ง n = (ceil − floor) / gap ชั้น · ทุนต่อชั้น cost = capital / n (USD)
  ชั้น k (0..n−1): ซื้อที่ B_k = floor + k·gap จำนวน a_k = cost / B_k BTC · ขายชั้นนั้นที่ S_k = B_k + gap
แต่ละชั้นถือ BTC ได้ 0..a_k (บัญชีไม้รายชั้น h_k จาก fill ที่ติด label ของชั้นนั้น):
  a_k − h_k > เศษ → มีออเดอร์ซื้อ (a_k − h_k) ที่ B_k   ·   h_k > เศษ → มีออเดอร์ขาย h_k ที่ S_k
  (ชั้นที่ fill บางส่วนมีทั้งสองฝั่ง แบบเดียวกับกริด RBSI) → ไม่ต้องจำว่า "ชั้นนี้ถืออยู่ไหม" แยก
ราคาทะลุชั้นไปแล้ว (B_k ≥ ask หรือ S_k ≤ bid) → วาง post-only ที่ราคา maker ดีที่สุด (ask−tick / bid+tick) ได้ราคาดีกว่าชั้น
  → ตอนเริ่มแบบ ratio: ชั้นเหนือราคาทั้งหมดยังว่าง = ทะลุแล้ว → ซื้อทยอยทีละ max_chase ชั้น จนถือครบ (เหมือน KZM ที่ backtest)
วางจริงเฉพาะชั้นใกล้ราคา: ฝั่งละ max_orders ชั้น + ต้องอยู่ในกรอบราคา min/max ของ Deribit (~±2% จาก index) ไม่งั้นถูกยกเลิกทิ้ง"""
from __future__ import annotations
import math
from dataclasses import dataclass, field

from .fv import floor_to
from .grid import fmtnum

EPS = 1e-12
PREFIX = "kz"


@dataclass(frozen=True)
class KZMConfig:
    floor: float                   # ราคาล่างสุดของกริด (ชั้นแรกซื้อที่นี่)
    ceil: float                    # ราคาบน (ชั้นสุดท้ายขายที่นี่)
    gap: float                     # ระยะชั้น (USD)
    capital: float                 # ทุนรวมทั้งกริด (USD) · ต่อชั้น = capital / n
    instrument: str = "BTC_USDC"
    tick: float = 0.01
    lot: float = 1e-8
    min_order_usd: float = 5.0     # ไม้เล็กกว่านี้ไม่วาง (เศษจากการปัด/fee)
    fee_rate: float = 0.0002       # maker ของ Deribit spot — กันเงินให้พอจ่าย fee ตอนซื้อ
    chase_bps: float = 5.0         # ไม้ไล่ราคา (ชั้นที่ราคาทะลุแล้ว) ห่างราคา maker ดีที่สุดเกินนี้ → วางใหม่
    max_orders: int = 5            # ออเดอร์รอที่ราคาชั้น ฝั่งละไม่เกินนี้ (ชั้นใกล้ราคาที่สุด)
    max_chase: int = 3             # ออเดอร์ไล่ราคา (ชั้นที่ทะลุแล้ว) ฝั่งละไม่เกินนี้ต่อครั้ง

    def __post_init__(self):
        errs = []
        if not self.ceil > self.floor > 0:
            errs.append("ceiling ต้องมากกว่า floor และ floor > 0")
        elif not self.gap > 0:
            errs.append("gap ต้องมากกว่า 0")
        else:
            n = (self.ceil - self.floor) / self.gap
            if abs(n - round(n)) > 1e-9 or round(n) < 2:
                errs.append("(ceiling − floor) ต้องหารด้วย gap ลงตัว และได้อย่างน้อย 2 ชั้น")
            elif round(n) > 5000:
                errs.append("ชั้นมากเกิน 5,000 — เพิ่ม gap")
            elif not self.capital > 0:
                errs.append("ทุนต้องมากกว่า 0")
            elif self.capital / round(n) < max(self.min_order_usd, 1):
                errs.append(f"ทุนต่อชั้น ${self.capital / round(n):,.2f} น้อยกว่าไม้เล็กสุด ${self.min_order_usd:,.2f} — เพิ่มทุนหรือ gap")
        for v in (self.floor, self.gap):
            if v > 0 and abs(v / self.tick - round(v / self.tick)) > 1e-6:
                errs.append(f"{v} ไม่ลงตัวกับ tick {self.tick}")
        if "_" not in self.instrument:
            errs.append("instrument ต้องเป็นคู่ spot เช่น BTC_USDC")
        if self.max_orders < 1 or self.max_chase < 1:
            errs.append("max_orders / max_chase ต้องอย่างน้อย 1")
        if errs:
            raise ValueError("; ".join(errs))

    @property
    def n(self) -> int:
        return int(round((self.ceil - self.floor) / self.gap))

    @property
    def cost(self) -> float:
        return self.capital / self.n

    @property
    def base(self) -> str:
        return self.instrument.split("_")[0]

    @property
    def quote(self) -> str:
        return self.instrument.split("_")[1]

    @property
    def fv_id(self) -> str:
        """รหัสบอท (ใช้ชื่อฟิลด์เดียวกับบอท Fixed Value เพราะใช้โครงบอท/ไฟล์สถานะร่วมกัน)"""
        s = f"KZM{self.base}{self.quote}-{fmtnum(self.floor)}-{fmtnum(self.ceil)}-G{fmtnum(self.gap)}-C{fmtnum(self.capital)}"
        return s.replace(".", "p")

    def buy_price(self, k: int) -> float:
        return round(self.floor + k * self.gap, 2)

    def sell_price(self, k: int) -> float:
        return round(self.floor + (k + 1) * self.gap, 2)

    def amount(self, k: int) -> float:
        """BTC เต็มชั้นของชั้น k"""
        return floor_to(self.cost / self.buy_price(k), self.lot)

    def label(self, key: str) -> str:
        return f"{PREFIX}_{self.fv_id}_{key}"


def key(k: int, side: str) -> str:
    return f"{k:04d}{'s' if side == 'sell' else 'b'}"


def parse_key(s: str):
    """'0123b' → (123, 'buy') · ผิดรูป → None"""
    if not s or len(s) < 2 or s[-1] not in "sb" or not s[:-1].isdigit():
        return None
    return int(s[:-1]), ("sell" if s[-1] == "s" else "buy")


def parse_label(cfg: KZMConfig, label: str | None):
    """key ถ้าเป็นออเดอร์ของบอทนี้ · None ถ้าไม่ใช่"""
    pre = f"{PREFIX}_{cfg.fv_id}_"
    if label and label.startswith(pre) and parse_key(label[len(pre):]):
        return label[len(pre):]
    return None


def full(cfg: KZMConfig, k: int, cap=None) -> float:
    """BTC เต็มชั้นของชั้น k: ปกติ cost / ราคาชั้น · ชั้นตั้งต้น (ซื้อตอนเริ่มแบบ ratio) = cost / ราคาตอนเริ่ม (ตาม KZM เดิม)"""
    return cap.get(k, cfg.amount(k)) if cap else cfg.amount(k)


def layer_need(cfg: KZMConfig, k: int, hk: float, cap=None) -> dict:
    """{side: จำนวน BTC} ที่ชั้น k ต้องมีออเดอร์ (ตัดเศษที่เล็กกว่า min_order_usd)"""
    want = {}
    buy = floor_to(full(cfg, k, cap) - hk, cfg.lot)
    if buy > 0 and buy * cfg.buy_price(k) >= cfg.min_order_usd:
        want["buy"] = buy
    sell = floor_to(hk, cfg.lot)
    if sell > 0 and sell * cfg.sell_price(k) >= cfg.min_order_usd:
        want["sell"] = sell
    return want


def needs(cfg: KZMConfig, h: dict, cap=None):
    """k -> {side: จำนวน BTC} ของทุกชั้นที่ต้องมีออเดอร์"""
    return {k: w for k in range(cfg.n) if (w := layer_need(cfg, k, h.get(k, 0.0), cap))}


def start_cap(cfg: KZMConfig, price: float) -> dict:
    """ชั้นตั้งต้นแบบ ratio: ชั้นที่ราคาซื้อ ≥ ราคาเริ่ม ถือชั้นละ cost / ราคาเริ่ม (เท่ากับเอาเงินชั้นละ cost ซื้อที่ราคาตลาด)"""
    a = floor_to(cfg.cost / price, cfg.lot)
    return {k: a for k in ideal_held(cfg, price)}


def ideal_held(cfg: KZMConfig, price: float) -> list[int]:
    """ชั้นที่ KZM แบบ ratio ควรถือเต็มที่ราคานี้ (ราคาซื้อของชั้น ≥ ราคา)"""
    return [k for k in range(cfg.n) if cfg.buy_price(k) >= price - 1e-9]


def allocate(cfg: KZMConfig, h: dict, amount: float, price: float, cap=None) -> dict:
    """เพิ่ม BTC `amount` เข้าชั้น (ใช้ตอนเริ่ม/รับยอดโอนเข้า) — เติมชั้นที่ควรถือจากล่างขึ้นบนก่อน
    แล้วชั้นใต้ราคาจากบนลงล่าง (ชั้นพวกนี้จะขายออกตามกริด) · คืน {k: จำนวนที่เติม} · เกินความจุทั้งกริด = ValueError"""
    order = [k for k in range(cfg.n) if cfg.buy_price(k) >= price - 1e-9] + \
            [k for k in range(cfg.n - 1, -1, -1) if cfg.buy_price(k) < price - 1e-9]
    left, add = amount, {}
    for k in order:
        if left <= 1e-10:
            break
        room = max(full(cfg, k, cap) - h.get(k, 0.0), 0.0)
        x = min(room, left)
        if x > 0:
            add[k] = x
            left -= x
    if left > 1e-8:
        raise ValueError(f"{cfg.base} เกินที่กริดรับได้ {left:.8f}")
    return add


def deallocate(cfg: KZMConfig, h: dict, amount: float, price: float) -> dict:
    """ถอน BTC ออกจากชั้น (ยอดโอนออก) — ถอนจากชั้นที่จะขายก่อน (ชั้นล่างสุดที่ถืออยู่) · คืน {k: จำนวนที่ถอน (บวก)}"""
    left, sub = amount, {}
    for k in range(cfg.n):
        if left <= 1e-10:
            break
        x = min(h.get(k, 0.0), left)
        if x > 0:
            sub[k] = x
            left -= x
    if left > 1e-8:
        raise ValueError(f"ถอน {cfg.base} เกินยอดในกริด {left:.8f}")
    return sub


@dataclass
class KZMDecision:
    actions: list = field(default_factory=list)      # ("cancel", oid, key, เหตุผล) · ("place", key, price, amount)
    info: dict = field(default_factory=dict)


def plan(cfg: KZMConfig, h: dict, cash: float, bid: float, ask: float, own: list, pending=(), band=None, cap=None) -> KZMDecision:
    """h = {k: BTC ของชั้น} (บัญชีไม้) · cash = USDC ทั้งหมดในบัญชีไม้ · own = list[(key, Order)] ของบอทนี้
    pending = key ที่เพิ่งส่งคำสั่ง (ห้ามส่งซ้ำ) · band = (min_price, max_price) ของ Deribit
    กติกากันซ้ำ: ต่อ key มีออเดอร์ได้ตัวเดียว · key ที่เพิ่งสั่งยกเลิก วางใหม่รอบถัดไปหลังยืนยันว่าหายแล้ว"""
    d = KZMDecision()
    mid = (bid + ask) / 2
    need = needs(cfg, h, cap)
    buy_best, sell_best = round(ask - cfg.tick, 2), round(bid + cfg.tick, 2)

    def target(k, side):
        """(ราคาที่ต้องการ, ไล่ราคาไหม)"""
        if side == "buy":
            lvl = cfg.buy_price(k)
            return (buy_best, True) if lvl >= ask - 1e-9 else (lvl, False)
        lvl = cfg.sell_price(k)
        return (sell_best, True) if lvl <= bid + 1e-9 else (lvl, False)

    # ลำดับความสำคัญต่อฝั่ง: ไล่ราคา (ชั้นที่ใกล้ราคาก่อน) แล้วชั้นรอที่ใกล้ราคาที่สุด
    ranked = {"buy": {"chase": [], "rest": []}, "sell": {"chase": [], "rest": []}}
    for k, w in need.items():
        for side in w:
            ranked[side]["chase" if target(k, side)[1] else "rest"].append(k)
    ranked["buy"]["chase"].sort()
    ranked["buy"]["rest"].sort(reverse=True)
    ranked["sell"]["chase"].sort(reverse=True)
    ranked["sell"]["rest"].sort()
    allow, keep_ok = set(), set()                     # allow = วางใหม่ได้ · keep_ok = มีอยู่แล้วคงไว้ได้ (กว้างกว่า กันยกเลิก/วางซ้ำตอนราคาแกว่ง)
    for side in ("buy", "sell"):
        for grp, lim in (("chase", cfg.max_chase), ("rest", cfg.max_orders)):
            for i, k in enumerate(ranked[side][grp]):
                if i < lim:
                    allow.add(key(k, side))
                if i < lim * 2:
                    keep_ok.add(key(k, side))

    def in_band(p):
        return not band or band[0] - 1e-9 <= p <= band[1] + 1e-9

    by_key: dict[str, list] = {}
    for kk, o in own:
        by_key.setdefault(kk, []).append(o)
    kept_buy_cost = 0.0
    handled = set()
    for kk, os_ in sorted(by_key.items()):
        pk = parse_key(kk)
        k, side = pk if pk else (-1, "")
        w = need.get(k, {}).get(side) if 0 <= k < cfg.n else None
        keep = None
        for o in sorted(os_, key=lambda o: o.order_id):
            if keep is not None:
                d.actions.append(("cancel", o.order_id, kk, "ซ้ำ"))
                continue
            if w is None:
                d.actions.append(("cancel", o.order_id, kk, "ชั้นนี้ไม่ต้องมี" + ("ซื้อ" if side == "buy" else "ขาย")))
                continue
            want, chasing = target(k, side)
            lvl = cfg.buy_price(k) if side == "buy" else cfg.sell_price(k)
            amt_ok = abs(o.remaining - w) <= max(3 * cfg.lot, 0.002 * w)
            if chasing:
                better = o.price <= lvl + 1e-9 if side == "buy" else o.price >= lvl - 1e-9
                px_ok = better and abs(o.price - want) <= want * cfg.chase_bps / 1e4 + 1e-9
            else:
                px_ok = abs(o.price - lvl) < cfg.tick / 2
            if not amt_ok:
                d.actions.append(("cancel", o.order_id, kk, "จำนวนไม่ตรงบัญชีไม้"))
            elif not px_ok:
                d.actions.append(("cancel", o.order_id, kk, "ไล่ราคา" if chasing else "ราคาไม่ตรงชั้น"))
            elif kk not in keep_ok:
                d.actions.append(("cancel", o.order_id, kk, "ไกลราคาเกิน"))
            else:
                keep = o
                if side == "buy":
                    kept_buy_cost += o.remaining * o.price * (1 + cfg.fee_rate)
        handled.add(kk)                                # key ที่มีออเดอร์อยู่ (คงไว้หรือเพิ่งยกเลิก) → ไม่วางใหม่รอบนี้

    free = cash - kept_buy_cost
    waiting, short = {}, None
    for side in ("sell", "buy"):
        for grp in ("chase", "rest"):
            for k in ranked[side][grp]:
                kk = key(k, side)
                if kk not in allow or kk in handled or kk in pending:
                    continue
                price, _ = target(k, side)
                if price <= 0:
                    continue
                if not in_band(price):
                    waiting[kk] = price                # นอกกรอบราคาของ Deribit → รอราคาเข้ามาใกล้
                    continue
                amt = need[k][side]
                if side == "buy":
                    c = amt * price * (1 + cfg.fee_rate)
                    if c > free + 1e-9:
                        short = (short or 0.0) + c     # USDC ไม่พอ → ข้ามฝั่งซื้อชั้นนี้ (ฝั่งขายทำงานต่อ)
                        continue
                    free -= c
                d.actions.append(("place", kk, price, amt))

    held = sum(h.values())
    rest_b, rest_s = ranked["buy"]["rest"], ranked["sell"]["rest"]
    d.info.update(mid=mid, btc=held, value=held * mid, layers_held=sum(1 for k in range(cfg.n) if h.get(k, 0.0) * cfg.sell_price(k) >= cfg.min_order_usd),
                  chase_buy=len(ranked["buy"]["chase"]), chase_sell=len(ranked["sell"]["chase"]),
                  next_buy=cfg.buy_price(rest_b[0]) if rest_b else None, next_sell=cfg.sell_price(rest_s[0]) if rest_s else None,
                  waiting_band=waiting, usdc_short=short, free_cash=free)
    return d


def cash_to_floor(cfg: KZMConfig, h: dict, cap=None, price: float | None = None) -> float:
    """USDC ที่ต้องใช้ถ้าราคาลงถึง floor (ซื้อทุกชั้นที่ยังไม่เต็ม รวม fee) · ชั้นที่ราคาทะลุแล้ว (เหนือ price) ซื้อที่ราคาตลาด"""
    px = lambda k: min(cfg.buy_price(k), price) if price else cfg.buy_price(k)
    return sum(w["buy"] * px(k) * (1 + cfg.fee_rate) for k, w in needs(cfg, h, cap).items() if "buy" in w)


def cash_runs_out(cfg: KZMConfig, h: dict, cash: float, price: float, cap=None) -> float | None:
    """ราคาที่ USDC หมด ถ้าราคาลงทางเดียวจาก price (ซื้อชั้นที่ยังไม่เต็มจากบนลงล่าง) · None = พอถึง floor"""
    c = cash
    for k in range(cfg.n - 1, -1, -1):
        w = layer_need(cfg, k, h.get(k, 0.0), cap).get("buy")
        if not w:
            continue
        px = min(cfg.buy_price(k), price)
        c -= w * px * (1 + cfg.fee_rate)
        if c < -1e-9:
            return px
    return None


def start_plan(cfg: KZMConfig, h: dict, price: float, cap=None) -> dict:
    """ภาพตอนเริ่มแบบ ratio ที่ราคา price: ต้องซื้อ/ขายทันทีเท่าไร (ชั้นที่ราคาทะลุแล้ว)"""
    need = needs(cfg, h, cap)
    buy = sum(w["buy"] for k, w in need.items() if "buy" in w and cfg.buy_price(k) >= price - 1e-9)
    sell = sum(w["sell"] for k, w in need.items() if "sell" in w and cfg.sell_price(k) <= price + 1e-9)
    held_after = sum(h.values()) + buy - sell
    return {"buy_btc": buy, "buy_usd": buy * price, "sell_btc": sell, "sell_usd": sell * price,
            "layers_ideal": len(ideal_held(cfg, price)), "btc_after": held_after, "btc_share": held_after * price / cfg.capital if cfg.capital else 0.0}
