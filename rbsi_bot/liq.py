# -*- coding: utf-8 -*-
"""ประมาณจุด liquidation ของ short inverse (BTC-PERPETUAL, Standard Margin, cross ในบัญชี)
port จาก ~/deribit-monitor/index.html (deribitLiqEstimate / deribitLiqAfterFills)
  equity(P) = eq + S·(1/P − 1/M)  ·  liquidate เมื่อ equity(P) = r·S/P
  r = MM rate สอบเทียบจาก maintenance_margin ของ position จริง (รวม tier แล้ว) ถ้าไม่มี position ใช้ค่า default
  equity ของ position อื่นถูกหักส่วน MM ออกก่อน (eqEff)"""
from __future__ import annotations


def mm_rate(pos_mm_btc: float, mark: float, size_usd: float, default: float) -> float:
    return pos_mm_btc * mark / size_usd if size_usd > 0 and pos_mm_btc > 0 else default


def eq_eff(equity: float, acc_mm: float, pos_mm: float) -> float:
    return equity - max(0.0, acc_mm - pos_mm)


def liq_short_now(size_usd: float, mark: float, eq: float, r: float, extra_usd: float = 0.0):
    """จุด liq ของ short ขนาด size_usd ด้วย equity (BTC) ที่ mark ปัจจุบัน · None = ไม่มีจุด liq"""
    if size_usd <= 0 or mark <= 0:
        return None
    den = size_usd / mark - eq
    return (size_usd * (1 - r) + extra_usd) / den if den > 0 else None


def liq_short_after_fills(size_usd: float, entry: float, mark: float, eq: float, r: float, adds: list, extra_usd: float = 0.0):
    """จุด liq ถ้า sell ที่รออยู่ทุกใบ (adds = [(price, amount_usd)]) fill หมด · wallet balance คงเดิม"""
    if mark <= 0:
        return None
    wb = eq + (size_usd * (1 / entry - 1 / mark) if size_usd > 0 and entry > 0 else 0.0)   # ถอด uPnL ณ mark ออก
    s2 = size_usd + sum(a for _, a in adds)
    inv2 = (size_usd / entry if size_usd > 0 and entry > 0 else 0.0) + sum(a / p for p, a in adds)
    if s2 <= 0:
        return None
    den = inv2 - wb
    return (s2 * (1 - r) + extra_usd) / den if den > 0 else None


def liq_long_after_fills(size_usd: float, entry: float, mark: float, eq: float, r: float, adds: list, extra_usd: float = 0.0):
    """จุด liq (ด้านล่าง) ของ long inverse ถ้า buy ที่รออยู่ทุกใบ (adds = [(price, amount_usd)]) fill หมด · wallet balance คงเดิม
    long L USD avg entry E: equity(P) = wb + L/E − L/P · liquidate เมื่อ equity(P) = r·L/P → P = (L(1+r) − extra) / (wb + Σq/p)
    (ตรง deribitLiqAfterFills ฝั่ง buy ของ ~/deribit-monitor · extra_usd = เงินส่วนเกินที่ช่วยค้ำ → liq ลงไปไกลขึ้น)"""
    if mark <= 0:
        return None
    wb = eq - (size_usd * (1 / entry - 1 / mark) if size_usd > 0 and entry > 0 else 0.0)   # ถอด uPnL ณ mark ออก
    l2 = size_usd + sum(a for _, a in adds)
    inv2 = (size_usd / entry if size_usd > 0 and entry > 0 else 0.0) + sum(a / p for p, a in adds)
    if l2 <= 0:
        return None
    num, den = l2 * (1 + r) - extra_usd, wb + inv2
    return num / den if num > 0 and den > 0 else None
