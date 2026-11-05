# -*- coding: utf-8 -*-
"""ทดสอบตรรกะกริดล้วน — ใช้สถานะจากจอจริงของผู้ใช้ (28 ก.ย. 2026) เป็นจุดตั้งต้น"""
import pytest
from rbsi_bot.grid import ZoneConfig, Snapshot, Order, plan, expected, m_range, parse_label

Z = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000)


def orders_for(m, skip=(), extra=()):
    out = []
    for k, (side, px) in expected(Z, m).items():
        if k in skip:
            continue
        out.append(Order(f"o{k}", side, px, 1000, 0, Z.label(k), side == "buy"))
    return out + list(extra)


def snap(m, mark, pos=None, ledger=None, **kw):
    return Snapshot(position=-(m * 1000) if pos is None else pos, mark=mark, best_bid=mark - 0.5, best_ask=mark + 0.5,
                    orders=kw.pop("orders", orders_for(m)), ledger={k: 1000.0 for k in range(1, m + 1)} if ledger is None else ledger, **kw)


def test_config():
    assert Z.n == 12 and Z.cap == 12000 and Z.label(4) == "rbsi_BTC82000-88000g500q1000_004"
    assert (Z.sell_price(4), Z.buy_price(4)) == (84000, 83500)
    with pytest.raises(ValueError):
        ZoneConfig("BTC-PERPETUAL", 82000, 88100, 500, 1000)
    with pytest.raises(ValueError):
        ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1005)
    assert parse_label(Z.label(7), Z.zone_id) == ("own", 7)
    assert parse_label("", Z.zone_id)[0] == "foreign"
    assert parse_label("rbsi_other_001", Z.zone_id)[0] == "other"


def test_m_range():
    assert m_range(Z, 83458) == (2, 4)          # จอจริง: position −3,000 → m = 3 อยู่ในช่วง
    assert m_range(Z, 81000) == (0, 0)          # ต่ำกว่ากรอบล่าง → ต้อง position 0
    assert m_range(Z, 89000) == (12, 12)        # เหนือกรอบบน → เต็ม


def test_screenshot_state_needs_nothing():
    d = plan(Z, snap(3, 83458))
    assert d.status == "ok" and d.actions == [] and d.m == 3
    buys = sorted(o.price for o in orders_for(3) if o.side == "buy")
    assert buys == [82000, 82500, 83000]       # ตรงกับจอ: buy 82,000–83,000 + sell 84,000–88,000


def test_sell_filled_places_buy_not_sell_again():
    """หัวใจของการกัน double: sell 84,000 fill แล้ว → วาง buy 83,500 เท่านั้น ไม่วาง sell 84,000 ซ้ำ"""
    led = {k: 1000.0 for k in range(1, 5)}
    d = plan(Z, snap(4, 84010, ledger=led, orders=orders_for(3, skip=(4,))))
    assert d.status == "ok"
    assert d.actions == [("limit", 4, "buy", 83500, 1000.0, True)]


def test_stale_position_is_flux():
    """position ยังไม่อัปเดต แต่ ledger เห็น fill แล้ว → รอ ไม่ลงมือ"""
    led = {k: 1000.0 for k in range(1, 5)}
    d = plan(Z, snap(3, 84010, ledger=led, orders=orders_for(3, skip=(4,))))
    assert d.status == "flux" and d.actions == []


def test_order_vanished_unconfirmed_is_flux():
    """ทั้ง position และ ledger ยังไม่อัปเดต แต่ออเดอร์หาย → ต้องยืนยันก่อน ห้ามวาง sell ซ้ำ"""
    d = plan(Z, snap(3, 83990, orders=orders_for(3, skip=(4,)), unresolved={4}))
    assert d.status == "flux" and d.actions == []


def test_pending_level_not_replaced():
    d = plan(Z, snap(3, 83458, orders=orders_for(3, skip=(4,)), pending={(4, "sell")}))
    assert d.status == "ok" and d.actions == []


def test_duplicate_sell_halts():
    dup = Order("dup", "sell", 84000, 1000, 0, Z.label(4))
    d = plan(Z, snap(3, 83458, orders=orders_for(3, extra=[dup])))
    assert d.status == "halt" and "เกินเพดาน" in d.reason


def test_resell_after_fill_would_halt():
    """ถ้ามีอะไรหลุดไปวาง sell 84,000 ซ้ำหลัง fill → ความเสี่ยงรวม 13,000 > 12,000 → halt"""
    led = {k: 1000.0 for k in range(1, 5)}
    again = Order("again", "sell", 84000, 1000, 0, Z.label(4))
    d = plan(Z, snap(4, 84010, ledger=led, orders=orders_for(3, skip=(4,)) + [again]))
    assert d.status == "halt"


def test_partial_fill_waits():
    part = Order("o4", "sell", 84000, 1000, 500, Z.label(4))
    led = {**{k: 1000.0 for k in range(1, 4)}, 4: 500.0}
    d = plan(Z, snap(3, 84000, pos=-3500, ledger=led, orders=orders_for(3, skip=(4,)) + [part]))
    assert d.status == "flux"                                   # fill 500 ยังไม่เข้าบัญชีไม้
    d = plan(Z, snap(3, 84000, pos=-3500, ledger=led, orders=orders_for(3, skip=(4,)) + [part], order_fills={"o4": 500.0}))
    assert d.status == "ok" and d.actions == [("limit", 4, "buy", 83500, 500.0, True)]   # ส่วนที่ fill แล้วได้ buy คืนทันที


def test_partial_level_without_order_completes_remainder():
    """sell ชั้น 4 fill ครึ่งแล้วออเดอร์ถูกยกเลิก → sell 500 ที่เหลือ + buy คืน 500 ที่ fill แล้ว"""
    led = {**{k: 1000.0 for k in range(1, 4)}, 4: 500.0}
    d = plan(Z, snap(3, 83900, pos=-3500, ledger=led, orders=orders_for(3, skip=(4,))))
    assert d.status == "ok" and sorted(d.actions) == sorted([("limit", 4, "sell", 84000, 500.0, False), ("limit", 4, "buy", 83500, 500.0, True)])
    d = plan(Z, snap(3, 84400, pos=-3500, ledger=led, orders=orders_for(3, skip=(4,))))       # ราคาเลย 84,000 แล้ว → sell ที่ ask
    assert sorted(d.actions) == sorted([("limit", 4, "sell", 84400.5, 500.0, False), ("limit", 4, "buy", 83500, 500.0, True)])


def test_gap_through_posts_at_best_bid():
    """sell 84,000 fill แล้วราคาร่วงผ่าน 83,500 ก่อนวาง buy → post-only ที่ bid (ถูกกว่า 83,500) ไม่ใช้ market"""
    led = {k: 1000.0 for k in range(1, 5)}
    d = plan(Z, snap(4, 83400, ledger=led, orders=orders_for(3, skip=(4,))))
    assert d.status == "ok" and d.actions == [("limit", 4, "buy", 83399.5, 1000.0, True)]


def test_chase_moves_stale_orders():
    """ออเดอร์ไล่ราคาที่ห่าง bid เกิน chase_tol ถูกยกเลิก แล้ววางใหม่ที่ bid ปัจจุบัน"""
    led = {k: 1000.0 for k in range(1, 5)}
    d = plan(Z, snap(4, 82700, ledger=led, orders=orders_for(4)))
    assert d.status == "ok" and sorted(a[0] + str(a[2]) for a in d.actions) == ["cancel3", "cancel4"]
    d2 = plan(Z, snap(4, 82700, ledger=led, orders=orders_for(4, skip=(3, 4))))
    assert sorted(d2.actions) == [("limit", 3, "buy", 82699.5, 1000.0, True), ("limit", 4, "buy", 82699.5, 1000.0, True)]
    near = Order("c4", "buy", 82690, 1000, 0, Z.label(4), True)                                 # ยังใกล้ bid (≤ 25) → คงไว้
    d3 = plan(Z, snap(4, 82700, ledger=led, orders=orders_for(4, skip=(3, 4)) + [near]))
    assert ("limit", 4, "buy", 82699.5, 1000.0, True) not in d3.actions and not any(a[0] == "cancel" for a in d3.actions)


def test_too_far_halts():
    led = {k: 1000.0 for k in range(1, 7)}
    d = plan(Z, snap(6, 82100, ledger=led, orders=orders_for(6)))
    assert d.status == "halt"


def test_zone_full_only_buys():
    led = {k: 1000.0 for k in range(1, 13)}
    d = plan(Z, snap(12, 89000, ledger=led, orders=orders_for(12)))
    assert d.status == "ok" and d.m == 12 and d.actions == []
    assert all(o.side == "buy" for o in orders_for(12))


def test_below_lower_position_zero():
    d = plan(Z, snap(0, 81000, ledger={}))
    assert d.status == "ok" and d.m == 0 and d.actions == []
    d2 = plan(Z, snap(1, 81000, ledger={1: 1000.0}, orders=orders_for(1, skip=(1,))))
    assert d2.actions == [("limit", 1, "buy", 80999.5, 1000.0, True)]                          # ยัง short → buy คืนที่ bid


def test_guards():
    assert plan(Z, snap(3, 83458, orders=orders_for(3) + [Order("m", "buy", 80000, 10, 0, "")])).status == "halt"   # ออเดอร์มือ
    assert plan(Z, snap(0, 83458, pos=1000, ledger={})).status == "halt"                                            # long


def test_partial_short_closed_when_price_falls_below():
    """กติกาผู้ใช้: ราคาชนกรอบล่าง position ต้อง 0 — แม้ชั้นบนจะเคย fill ค้างครึ่งเดียว"""
    led = {6: 500.0}
    part = Order("o6", "sell", 85000, 1000, 500, Z.label(6))
    orders = [o for o in orders_for(0) if o.order_id != "o6"] + [part]
    d = plan(Z, snap(0, 81700, pos=-500, ledger=led, orders=orders, order_fills={"o6": 500.0}))
    assert d.status == "ok" and d.actions == [("limit", 6, "buy", 81699.5, 500.0, True)]


def test_non_contiguous_levels_self_heal():
    """ชั้น 4 short แต่ชั้น 3 ว่าง (buy ชั้น 4 วางไม่ทันตอนราคาร่วง) → buy ชั้น 4 ที่ bid + วาง sell ชั้น 3"""
    led = {1: 1000.0, 2: 1000.0, 4: 1000.0}
    d = plan(Z, snap(3, 83458, ledger=led, orders=[o for o in orders_for(2) if o.order_id not in ("o3", "o4")]))
    assert d.status == "ok"
    assert ("limit", 4, "buy", 83457.5, 1000.0, True) in d.actions
    assert ("limit", 3, "sell", 83500, 1000.0, False) in d.actions


def test_never_market():
    """ทุกคำสั่งเป็น limit post-only"""
    for m, px in ((3, 83458), (4, 83400), (0, 81000), (12, 90000)):
        led = {k: 1000.0 for k in range(1, m + 1)}
        d = plan(Z, snap(m, px, ledger=led, orders=[]))
        assert all(a[0] in ("limit", "cancel") for a in d.actions)


def test_missing_orders_all_placed_on_adoption():
    d = plan(Z, snap(3, 83458, orders=[]))
    assert d.status == "ok" and len(d.actions) == 12
    assert sum(1 for a in d.actions if a[2] == "buy") == 3
