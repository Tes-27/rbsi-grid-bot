# -*- coding: utf-8 -*-
"""ตรรกะกริดล้วน ๆ (ไม่แตะเครือข่าย ทดสอบได้ทั้งหมด)

โมเดล zone: กรอบ [lower, upper] แบ่ง n = (upper − lower) / gap ชั้น · ทิศทาง short (เดิม) หรือ long (กระจกเงา)
  short: ชั้น k (1..n) เปิดด้วย sell ที่ lower + k·gap  ปิดด้วย buy  (reduce-only) ที่ lower + (k−1)·gap
         position = 0 ที่กรอบล่าง · short เต็ม n×size ที่กรอบบน
  long : ชั้น k (1..n) เปิดด้วย buy  ที่ upper − k·gap  ปิดด้วย sell (reduce-only) ที่ upper − (k−1)·gap
         position = 0 ที่กรอบบน · long เต็ม n×size ที่กรอบล่าง
  ชั้นที่ "ถืออยู่" มีออเดอร์ปิดรอ · ชั้นที่ "ว่าง" มีออเดอร์เปิดรอ · ถือ m ชั้น → |position| = m × size
  sell_price(k) / buy_price(k) = ราคาออเดอร์ฝั่ง sell / buy ของชั้น k (ใช้ได้ทั้งสองทิศ)

แหล่งความจริง 3 ทางที่ต้องตรงกัน ("ตรวจคำตอบ"):
  1) position จาก exchange
  2) บัญชีไม้รายชั้น (ledger) = baseline ตอนรับช่วง + ผลรวม fill ที่ติด label ของบอท
  3) open orders ที่ติด label ของบอท
ถ้าไม่ตรง = ข้อมูลยังไม่นิ่ง (flux) → ไม่ลงมือ · ถ้าผิดกติกา = halt
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field

LABEL_PREFIX = "rbsi"
EPS = 1e-6
DERIBIT_MAX_OPEN = 100     # Deribit: ออเดอร์ค้างได้ไม่เกิน 100 ตัวต่อสัญญาต่อ subaccount (max_open_orders_per_instrument 11020)
WINDOW_SLACK = 6           # หน้าต่างออเดอร์: ตัวที่วางแล้วเก็บไว้ได้ถึงอันดับ max_orders + slack ก่อนยกเลิก (กันวาง-ยกเลิกวนที่ขอบหน้าต่าง)


def fmtnum(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


@dataclass(frozen=True)
class ZoneConfig:
    instrument: str
    lower: float
    upper: float
    gap: float
    size: int                  # USD ต่อชั้น (ทวีคูณของ contract)
    tick: float = 0.5
    contract: int = 10
    tol_frac: float = 0.25     # ระยะเผื่อความสอดคล้องราคา (สัดส่วนของ gap)
    max_catchup: int = 2       # จำนวนชั้น/ฝั่งที่ตามราคาไม่ทันพร้อมกันสูงสุด เกินนี้ = หยุด
    chase_frac: float = 0.05   # ไล่ราคาแบบ post-only: เลื่อนออเดอร์เมื่อ bid/ask หนีเกิน chase_frac × gap
    direction: str = "short"   # 'short' (เดิม) | 'long'
    max_orders: int = 90       # หน้าต่างออเดอร์: วางเฉพาะออเดอร์ที่ใกล้ราคาที่สุดเท่านี้ตัว (0 = วางทุกชั้น) — ชั้นไกลวางเมื่อราคาเข้าใกล้

    def __post_init__(self):
        errs = []
        if self.direction not in ("short", "long"):
            errs.append("direction ต้องเป็น short หรือ long")
        if not (self.upper > self.lower > 0):
            errs.append("upper ต้องมากกว่า lower และ lower > 0")
        if not self.gap > 0:
            errs.append("gap ต้องมากกว่า 0")
        else:
            n = (self.upper - self.lower) / self.gap
            if abs(n - round(n)) > 1e-9 or round(n) < 1:
                errs.append("(upper − lower) ต้องหารด้วย gap ลงตัว")
        if self.size < self.contract or self.size % self.contract:
            errs.append(f"size ต้องเป็นทวีคูณของ {self.contract} USD")
        for v in (self.lower, self.upper, self.gap):
            if abs(v / self.tick - round(v / self.tick)) > 1e-9:
                errs.append(f"{v} ไม่ลงตัวกับ tick {self.tick}")
        if not 0 <= self.tol_frac < 0.5:
            errs.append("tol_frac ต้องอยู่ใน [0, 0.5)")
        if not 0 < self.chase_frac < 0.5:
            errs.append("chase_frac ต้องอยู่ใน (0, 0.5)")
        if self.max_orders and not 4 <= self.max_orders <= DERIBIT_MAX_OPEN - WINDOW_SLACK:
            errs.append(f"max_orders ต้องเป็น 0 (ไม่จำกัด) หรือ 4–{DERIBIT_MAX_OPEN - WINDOW_SLACK} (Deribit รับออเดอร์ค้างได้ {DERIBIT_MAX_OPEN} ตัวต่อสัญญา)")
        if errs:
            raise ValueError("; ".join(errs))

    @property
    def n(self) -> int:
        return int(round((self.upper - self.lower) / self.gap))

    @property
    def cap(self) -> int:
        return self.n * self.size

    @property
    def zone_id(self) -> str:
        base = self.instrument.split("-")[0]
        # long ต่อท้าย L (short คงรูปเดิม — ไฟล์สถานะ/label ของ zone ที่รันอยู่ไม่เปลี่ยน)
        return f"{base}{fmtnum(self.lower)}-{fmtnum(self.upper)}g{fmtnum(self.gap)}q{self.size}" + ("L" if self.is_long else "")

    def label(self, k: int) -> str:
        return f"{LABEL_PREFIX}_{self.zone_id}_{k:03d}"          # ใช้แค่ a-z 0-9 - _ (ปลอดภัยกับ label ของ Deribit)

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def open_side(self) -> str:
        """ฝั่งที่เพิ่ม position (short: sell · long: buy) — อีกฝั่งเป็น reduce-only"""
        return "buy" if self.is_long else "sell"

    @property
    def close_side(self) -> str:
        return "sell" if self.is_long else "buy"

    @property
    def word(self) -> str:
        return "long" if self.is_long else "short"

    def held(self, position: float) -> float:
        """ขนาดที่ถือในทิศของ zone (บวก = ถูกฝั่ง · ลบ = ถือผิดฝั่ง)"""
        return position if self.is_long else -position

    def sell_price(self, k: int) -> float:
        return self.upper - (k - 1) * self.gap if self.is_long else self.lower + k * self.gap

    def buy_price(self, k: int) -> float:
        return self.upper - k * self.gap if self.is_long else self.lower + (k - 1) * self.gap

    def price(self, k: int, side: str) -> float:
        return self.sell_price(k) if side == "sell" else self.buy_price(k)

    def open_price(self, k: int) -> float:
        return self.price(k, self.open_side)

    def close_price(self, k: int) -> float:
        return self.price(k, self.close_side)


def parse_label(label: str | None, zone_id: str):
    """('own', k) | ('other', None) = ของบอท zone อื่น | ('foreign', None) = ไม่ใช่ของบอท"""
    if not label or not label.startswith(LABEL_PREFIX + "_"):
        return "foreign", None
    parts = label.split("_")
    if len(parts) != 3 or parts[1] != zone_id:
        return "other", None
    try:
        return "own", int(parts[2])
    except ValueError:
        return "other", None


@dataclass
class Order:
    order_id: str
    side: str            # 'buy' | 'sell'
    price: float
    amount: float
    filled: float = 0.0
    label: str = ""
    reduce_only: bool = False

    @property
    def remaining(self) -> float:
        return self.amount - self.filled


@dataclass
class Snapshot:
    position: float                  # USD มีเครื่องหมาย (short < 0)
    mark: float
    best_bid: float | None
    best_ask: float | None
    orders: list                     # list[Order] ของ instrument นี้
    ledger: dict                     # k -> ยอด short สุทธิ (USD) ของชั้นนั้น
    unresolved: set = field(default_factory=set)   # ชั้นที่ออเดอร์หายไปแต่ยังยืนยันไม่ได้
    pending: set = field(default_factory=set)      # (ชั้น, ฝั่ง) ที่เพิ่งส่งออเดอร์ (ห้ามส่งซ้ำ)
    seen_full: bool = False
    order_fills: dict = field(default_factory=dict)  # order_id -> ยอด fill ที่เข้า ledger แล้ว
    last_side: dict = field(default_factory=dict)    # k -> ฝั่งของ fill ล่าสุดในชั้นนั้น
    adopted: dict = field(default_factory=dict)      # order_id -> k ของออเดอร์มือที่บอทรับมาเป็นของตัวเอง (ไม่มี label)


@dataclass
class Decision:
    status: str                      # 'ok' | 'flux' | 'halt'
    reason: str = ""
    actions: list = field(default_factory=list)
    m: int | None = None
    info: dict = field(default_factory=dict)


def m_range(cfg: ZoneConfig, price: float):
    """ช่วงจำนวนชั้นที่ถือที่สอดคล้องกับราคา (มีระยะเผื่อ tol)
    lo = ชั้นที่ราคาผ่านราคาเปิดไปแล้วเกิน tol (ต้องถือแน่นอน)
    hi = ชั้นที่ราคาปิดยังไม่ถูกแตะเกิน tol (ยังถือได้)
    short วัดระยะจากกรอบล่างขึ้นไป · long วัดระยะจากกรอบบนลงมา"""
    tol = cfg.tol_frac * cfg.gap
    d = cfg.upper - price if cfg.is_long else price - cfg.lower
    lo = math.floor((d - tol) / cfg.gap + 1e-12)
    hi = math.ceil((d + tol) / cfg.gap - 1e-12)
    return max(0, min(cfg.n, lo)), max(0, min(cfg.n, hi))


def expected(cfg: ZoneConfig, m: int) -> dict:
    """k -> (side, price) ของออเดอร์ที่ควรมีเมื่อถืออยู่ m ชั้น"""
    return {k: (cfg.close_side, cfg.close_price(k)) if k <= m else (cfg.open_side, cfg.open_price(k)) for k in range(1, cfg.n + 1)}


def window_ranks(req: dict, mark: float) -> dict:
    """(ชั้น, ฝั่ง) -> อันดับความใกล้ราคา (0 = ใกล้สุด) ของออเดอร์ที่ต้องมีทุกตัว · เสมอกันเรียงตามราคาแล้วชั้น (ผลแน่นอน)"""
    items = sorted((abs(px - mark), px, k, side) for k, sides in req.items() for side, (px, _) in sides.items())
    return {(k, side): i for i, (_, _, k, side) in enumerate(items)}


def halt(reason, **info):
    return Decision("halt", reason, info=info)


def flux(reason, **info):
    return Decision("flux", reason, info=info)


def plan(cfg: ZoneConfig, s: Snapshot) -> Decision:
    size, n = cfg.size, cfg.n
    own, foreign, other = [], [], []
    for o in s.orders:
        kind, k = parse_label(o.label, cfg.zone_id)
        if kind == "foreign" and o.order_id in s.adopted:
            kind, k = "own", s.adopted[o.order_id]
        (own if kind == "own" else foreign if kind == "foreign" else other).append((k, o))
    if foreign:
        return halt(f"มีออเดอร์ที่บอทไม่ได้วาง {len(foreign)} ตัวบน {cfg.instrument} — ถ้าเป็นไม้ที่ตรงสูตร เริ่มบอทใหม่ด้วย --adopt-manual",
                    orders=[o.order_id for _, o in foreign])
    if other:
        return halt(f"มีออเดอร์ของ zone อื่น {len(other)} ตัวใน subaccount นี้")

    S = cfg.held(s.position)                           # ขนาดที่ถือในทิศของ zone
    opn, cls = cfg.open_side, cfg.close_side
    if S < -EPS:
        return halt(f"position ผิดฝั่ง ({s.position:+,.0f}) — zone นี้เป็น {cfg.word}")
    if S > cfg.cap + EPS:
        return halt(f"position {S:,.0f} เกินเพดาน zone {cfg.cap:,}")
    # ข้อมูลต้องนิ่งก่อนตัดสินว่า "ผิดกติกา": รายการออเดอร์ / position / trade มาไม่พร้อมกัน
    # (เจอจริง 7 ต.ค. 2026: position อัปเดตแล้วแต่ออเดอร์ที่เพิ่ง fill ยังค้างในรายการ → ด่านเพดาน/ด่าน buy เตือนผิด แล้ว halt ยกเลิก sell หมด)
    for k, o in own:                                   # ยอด fill ของออเดอร์ที่เห็น ต้องเข้าบัญชีไม้แล้ว (ออเดอร์มักมาเร็วกว่า fill)
        if abs(s.order_fills.get(o.order_id, 0.0) - o.filled) > EPS:
            return flux(f"fill ของออเดอร์ชั้น {k} ยังไม่เข้าบัญชีไม้ ({o.filled:,.0f})")
    led_total = sum(s.ledger.values())
    if abs(led_total - S) > EPS:
        return flux(f"position {S:,.0f} ≠ บัญชีไม้ {led_total:,.0f} (รอข้อมูล fill)", ledger=led_total, position=S)

    open_rem = sum(o.remaining for k, o in own if o.side == opn)
    close_rem = sum(o.remaining for k, o in own if o.side == cls)
    # ด่านสุดท้าย: ที่ถือ + ออเดอร์เปิดที่รออยู่ ต้องไม่เกินเพดาน (ถ้าเกิน = มีออเดอร์ซ้ำ/double)
    if S + open_rem > cfg.cap + EPS:
        return halt(f"ความเสี่ยงรวม {S + open_rem:,.0f} เกินเพดาน {cfg.cap:,} — อาจมีออเดอร์ซ้ำ", exposure=S + open_rem)
    if close_rem > S + EPS:
        # ออเดอร์ปิดเป็น reduce-only (พลิกฝั่งไม่ได้) → รอให้นิ่ง ค้างเกิน flux_halt_sec ค่อย halt
        return flux(f"{cls} ที่รอ {close_rem:,.0f} มากกว่า {cfg.word} {S:,.0f} (รอรายการออเดอร์อัปเดต)")
    if s.unresolved:
        return flux(f"รอยืนยันสถานะออเดอร์ที่หายไปของชั้น {sorted(s.unresolved)}")

    by_k: dict[int, list] = {}
    actions = []
    for k, o in own:
        if not 1 <= k <= n:
            actions.append(("cancel", o.order_id, k, "ชั้นนอกกรอบ"))
            continue
        by_k.setdefault(k, []).append(o)

    # ออเดอร์ที่ต้องมีรายชั้น (รายฝั่ง) จากยอดที่ถือสุทธิของชั้น net:
    #   ออเดอร์เปิด (size − net) ที่ราคาเปิด  ·  ออเดอร์ปิด (net) reduce-only ที่ราคาปิด
    #   ชั้นเต็ม = ปิดอย่างเดียว · ชั้นว่าง = เปิดอย่างเดียว · ชั้นที่ fill บางส่วน = มีทั้งสองฝั่ง
    #   (ส่วนที่ fill แล้วได้ออเดอร์ปิดทันที ส่วนที่ยังไม่ fill รอเปิดต่อ → position ตรงสูตรเสมอ)
    req, net_of = {}, {}
    for k in range(1, n + 1):
        net = s.ledger.get(k, 0.0)
        if net < -EPS or net > size + EPS:
            return halt(f"บัญชีไม้ชั้น {k} ผิดปกติ ({net:,.0f})")
        net_of[k] = net
        req[k] = {}
        if size - net > EPS:
            req[k][opn] = (cfg.open_price(k), size - net)
        if net > EPS:
            req[k][cls] = (cfg.close_price(k), net)
    held_k = [k for k in range(1, n + 1) if net_of[k] > EPS]
    m = int(round(S / size)) if abs(S / size - round(S / size)) < 1e-9 else None
    lo, hi = m_range(cfg, s.mark)
    info = dict(m=m, lo=lo, hi=hi, exposure=S + open_rem, levels_short=held_k)   # levels_short = ชั้นที่ถือ (ชื่อเดิม — หน้าเว็บ/สถานะใช้อยู่)

    # ทุกออเดอร์เป็น post-only (maker) — ไม่มี market order
    # ราคาที่ต้องการของแต่ละชั้น/ฝั่ง = ราคาชั้น ถ้ายังไม่ทะลุ book
    #   ถ้าราคาวิ่งทะลุชั้นไปแล้ว → วางที่ bid (ฝั่ง buy) / ask (ฝั่ง sell) ดีที่สุดแทน (ได้ราคาดีกว่าราคาชั้น)
    #   และเลื่อนตาม (chase) เมื่อ bid/ask หนีไปเกิน chase_tol · ราคากลับมา = กลับไปวางที่ราคาชั้น
    tol = cfg.tol_frac * cfg.gap
    chase_tol = cfg.chase_frac * cfg.gap
    late = [(k, side) for k in range(1, n + 1) for side in req[k]
            if (side == "buy" and s.mark <= cfg.buy_price(k) - tol) or (side == "sell" and s.mark >= cfg.sell_price(k) + tol)]
    info["late"] = late
    # หน้าต่างออเดอร์ (Deribit จำกัด 100 ตัวต่อสัญญา): วางใหม่เฉพาะอันดับ < max_orders · ตัวที่วางแล้วเก็บได้ถึง max_orders + slack
    # position/บัญชีไม้/สูตรรายชั้นยังคิดครบทุกชั้น — ชั้นที่ไม่มีออเดอร์ไม่มีทางเกิด fill จึงไม่กระทบด่านกัน double
    total_req = sum(len(v) for v in req.values())
    N = cfg.max_orders if cfg.max_orders and total_req > cfg.max_orders else 0
    rank = window_ranks(req, s.mark) if N else {}
    info["want_orders"] = min(N, total_req) if N else total_req
    if len(late) > cfg.max_catchup:
        return halt(f"ราคา {s.mark:,.1f} ทิ้งห่างสถานะ {len(late)} รายการ (> {cfg.max_catchup}) {late}", **info)

    def target(k, side):
        """(ราคาที่ต้องการ, เป็นการไล่ราคาหรือไม่)"""
        lvl = req[k][side][0]
        if side == "buy" and s.best_ask is not None and lvl >= s.best_ask:
            return (s.best_bid if s.best_bid is not None else lvl - cfg.tick), True
        if side == "sell" and s.best_bid is not None and lvl <= s.best_bid:
            return (s.best_ask if s.best_ask is not None else lvl + cfg.tick), True
        return lvl, False

    for k in range(1, n + 1):
        os_ = by_k.get(k, [])
        for side in ("sell", "buy"):
            mine = [o for o in os_ if o.side == side]
            if side not in req[k]:
                actions += [("cancel", o.order_id, k, f"ชั้นนี้ไม่ต้องมี {side}") for o in mine]
                continue
            r = rank.get((k, side), 0)
            if N and r >= N + WINDOW_SLACK:
                actions += [("cancel", o.order_id, k, f"นอกหน้าต่างออเดอร์ (อันดับ {r + 1} > {N + WINDOW_SLACK})") for o in mine]
                continue
            lvl, amt = req[k][side]
            want, chasing = target(k, side)

            def ok_price(o):
                if not chasing:
                    return abs(o.price - lvl) <= EPS
                # ไล่ราคา: ต้องไม่แย่กว่าราคาชั้น และยังอยู่ใกล้ bid/ask ในระยะ chase_tol
                better = o.price <= lvl + EPS if side == "buy" else o.price >= lvl - EPS
                return better and abs(o.price - want) <= chase_tol + EPS
            good = [o for o in mine if ok_price(o) and abs(o.remaining - amt) <= EPS]
            for o in mine:
                if o not in good:
                    why = "ราคาหนี — เลื่อนตาม" if chasing and abs(o.remaining - amt) <= EPS else f"ผิดชุด (ต้องเป็น {side} {want:,.1f} × {amt:,.0f})"
                    actions.append(("cancel", o.order_id, k, why))
            for o in good[1:]:
                actions.append(("cancel", o.order_id, k, "ออเดอร์ซ้ำในชั้นเดียวกัน"))
            if mine or (k, side) in s.pending or (N and r >= N):
                continue
            actions.append(("limit", k, side, want, float(amt), side == cls))
    return Decision("ok", "", actions, m, info)
