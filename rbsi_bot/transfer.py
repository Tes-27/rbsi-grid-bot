# -*- coding: utf-8 -*-
"""โอนเงินออกจาก sub ได้เท่าไร โดยออเดอร์ของบอทไม่ถูก Deribit ยกเลิก (BTC-PERPETUAL inverse, Standard Margin)

กติกา Deribit (หน้า "Margin calls"): initial margin > margin balance → ออเดอร์ที่เพิ่ม margin ถูกยกเลิก + วางใหม่ไม่ได้
  (ออเดอร์ reduce-only ไม่เพิ่ม margin) · maintenance margin > margin balance → liquidate (MM = 2/3 × IM)

IM ของออเดอร์ค้าง (ไขจาก API จริง 20–21 ต.ค. 2026 — ตรง initial_margin ของบัญชีไม่ถึง 0.02%):
  แยกฝั่ง buy / sell · เดินราคาจาก **index** (ไม่ใช่ mark) ผ่านออเดอร์ฝั่งนั้นทีละใบ (ใกล้ → ไกล)
  ณ แต่ละราคา p: ต้องใช้ = r × |position หลัง fill| / p + ขาดทุนสะสมจาก index ถึง p
  IM บัญชี = max(ฝั่ง buy, ฝั่ง sell) (บัญชีเปล่า = r × q/p ตรง ๆ, r = 2% ที่ขนาดเล็ก)
  (เดิมเริ่มจาก mark → คลาด ~0.37% = |position| × premium: premium บวกทำให้ IM ของ short ที่มี sell รอข้างบนสูงขึ้น)
→ available = equity(mark) − IM จึงหักขาดทุนระหว่างทางไว้แล้ว · ราคาวิ่งในกรอบไม่ทำให้ available ลด
  ที่ลดจริง: premium (mark − index) แกว่งไปทางเสียเปรียบ · fee · funding · โอนออก · หน้าต่างออเดอร์วางชั้นไกลเพิ่ม
เผื่อเงินเป็น BTC (ไม่ใช่ %): IM สำหรับวาง short/long เพิ่ม LAYERS_BUF ไม้ + premium แกว่งไปทางเสียเปรียบถึง PREMIUM_BUF
  (เดิมเผื่อแบบ IM ≤ 95% — Beta1 ต้องกันถึง 0.005 BTC เพราะ IM โตเมื่อราคาลง ทั้งที่ available คงที่)
บทเรียน 20 ต.ค. 2026: โอนออกเท่า available พอดี (Beta1 0.00067308) → IM 100% ใน 36 วิ ได้อีเมล margin call · Deribit ยอมให้โอนจนชน 100%"""
from __future__ import annotations
import math
from .grid import ZoneConfig, expected

IM_RATE = 0.02                      # Deribit BTC perp tier 1: 50x ที่ขนาดเล็ก (สอบเทียบจาก position จริงถ้ามี)
LAYERS_BUF = 1                      # เผื่อ IM ให้วางไม้เปิดเพิ่มได้อีกกี่ไม้
PREMIUM_BUF = 0.002                 # premium (mark/index − 1) แกว่งไปทางเสียเปรียบได้ถึง 0.2% (short: บวก · long: ลบ)


def im_side(pos: float, start: float, orders: list, side: str, r: float = IM_RATE) -> float:
    """IM ฝั่งเดียว · pos = USD (+long −short) · start = ราคาเริ่มเดิน (index) · orders = [(price, amount_usd)] ของฝั่ง side"""
    os_ = sorted(orders, key=lambda o: -o[0] if side == "buy" else o[0])
    sgn = 1 if side == "buy" else -1
    q, pnl, last, best = pos, 0.0, start, r * abs(pos) / start
    for p, amt in os_:
        pnl += q * (1 / last - 1 / p)                   # inverse: PnL ของ q USD เมื่อราคาเดิน last → p
        last, q = p, q + sgn * amt
        best = max(best, r * abs(q) / p - pnl)
    return best


def im_account(pos: float, mark: float, buys: list, sells: list, r: float = IM_RATE, index: float | None = None) -> float:
    s = index or mark
    return max(im_side(pos, s, buys, "buy", r), im_side(pos, s, sells, "sell", r))


def held_at(cfg: ZoneConfig, price: float) -> int:
    d = (cfg.upper - price) if cfg.is_long else (price - cfg.lower)
    return max(0, min(cfg.n, math.floor(d / cfg.gap + 1e-9)))


def pos_at(cfg: ZoneConfig, price: float) -> float:
    m = held_at(cfg, price)
    return (m if cfg.is_long else -m) * cfg.size


def ladder(cfg: ZoneConfig, price: float):
    """ออเดอร์ที่บอทวาง ณ ราคานี้ (ตามสูตร + หน้าต่าง max_orders) → (buys, sells) แต่ละตัว (price, amount)"""
    req = sorted((abs(px - price), px, side) for side, px in expected(cfg, held_at(cfg, price)).values())
    if cfg.max_orders:
        req = req[:cfg.max_orders]
    return [(px, cfg.size) for _, px, s in req if s == "buy"], [(px, cfg.size) for _, px, s in req if s == "sell"]


def scan(cfg: ZoneConfig, mark: float, equity: float, r: float = IM_RATE, premium: float = 0.0) -> list[dict]:
    """available ตามราคา (mark) ตั้งแต่กรอบล่าง − 2 ชั้น ถึงกรอบบน + 2 ชั้น · index = mark / (1 + premium)
    equity เดินตาม position ของบอท (ไม่รวม funding/fee)"""
    lo, hi = cfg.lower - 2 * cfg.gap, cfg.upper + 2 * cfg.gap
    pts = sorted({mark, *(lo + i * cfg.gap / 4 for i in range(int((hi - lo) / (cfg.gap / 4)) + 1))})
    i0 = pts.index(mark)
    eq = {mark: equity}
    for seq in (pts[i0:], pts[i0::-1]):                 # เดินออกจาก mark ทั้งสองทาง · ขอบชั้นตรงจุดสแกนเสมอ → position คงที่ในแต่ละช่วง
        e = equity
        for a, b in zip(seq, seq[1:]):
            e += pos_at(cfg, (a + b) / 2) * (1 / a - 1 / b)
            eq[b] = e
    out = []
    for p in pts:
        buys, sells = ladder(cfg, p)
        im = im_account(pos_at(cfg, p), p, buys, sells, r, index=p / (1 + premium))
        out.append({"price": p, "equity": eq[p], "im": im, "available": eq[p] - im,
                    "opening": len(buys) if cfg.is_long else len(sells), "mm": r * 2 / 3 * abs(pos_at(cfg, p)) / p})
    return out


def transferable(cfg: ZoneConfig, a: dict, layers: int = LAYERS_BUF, prem: float = PREMIUM_BUF) -> dict:
    """a = ผลจาก ui.account() (equity, available, withdrawable, position, pos_im, mark, index)
    โอนได้ T: หลังโอน ทุกราคาในกรอบที่ยังมีออเดอร์เปิด (premium ไปทางเสียเปรียบ prem) ยังเหลือ available ≥ IM ของ layers ไม้
    และไม่เกินยอดที่ Deribit ให้ถอน/โอนได้ · ติดลบ = ควรเติมเงิน (topup)"""
    mark, eq, size = float(a["mark"]), float(a["equity"]), abs(float(a.get("position") or 0))
    r = float(a["pos_im"]) * mark / size if size > 0 and float(a.get("pos_im") or 0) > 0 else IM_RATE
    p_now = mark / float(a["index"]) - 1 if a.get("index") else 0.0
    adv = max(prem, p_now) if not cfg.is_long else min(-prem, p_now)     # short เสียเปรียบเมื่อ premium บวก · long เมื่อลบ
    # สอบเทียบ: จำลอง ณ mark ด้วย premium จริงเทียบ Deribit (position/ออเดอร์จริงต่างสูตรได้ ±ชั้น) → ถ้าจำลองดีกว่าจริง เลื่อนลงให้เท่าจริง
    sim_now = next(x for x in scan(cfg, mark, eq, r, p_now) if x["price"] == mark)
    off = min(0.0, float(a["available"]) - sim_now["available"])
    rows = scan(cfg, mark, eq, r, adv)
    inz = [x for x in rows if x["opening"] > 0] or rows
    layer = lambda p: layers * r * cfg.size / p                            # IM ของไม้เปิดเพิ่ม layers ไม้ ณ ราคานั้น
    room = lambda x: x["available"] + off - layer(x["price"])              # โอนออกได้เท่านี้ แล้วยังเหลือเผื่อพอดี
    worst = min(inz, key=room)
    need = room(worst)
    w = a.get("withdrawable")
    t = max(0.0, min([need] + ([float(w)] if w is not None else [])))
    bad = [x["price"] for x in rows if x["equity"] - t < x["mm"]]          # liquidate (equity < MM) หลังโอน t — ราคาที่ใกล้ mark ที่สุดในช่วงสแกน
    im_now = eq - float(a["available"])
    return {"transferable": t, "topup": max(0.0, -need), "layers": layers, "layer_btc": layer(mark), "prem": prem, "prem_now": p_now,
            "im_pct": im_now / eq if eq > 0 else None, "available": float(a["available"]), "withdrawable": w,
            "worst_available": worst["available"] + off, "worst_price": worst["price"], "im_rate": r, "offset": off, "mark": mark,
            "liq_after": min(bad, key=lambda p: abs(p - mark)) if bad else None}
