# -*- coding: utf-8 -*-
"""หน้าต่างออเดอร์ (9 ต.ค. 2026): Deribit รับออเดอร์ค้างได้ไม่เกิน 100 ตัวต่อสัญญาต่อ sub (11020) แต่ zone gap 100 มี 180 ชั้น
บอทวางเฉพาะ max_orders (90) ตัวที่ใกล้ราคาที่สุด · ตัวที่วางแล้วเก็บได้ถึงอันดับ 96 (slack 6) · ไกลกว่านั้นยกเลิก
สถานการณ์จริง HFT-1: long 77,000–95,000 gap 100 × 10 · ราคา 83,048 · long 119 ชั้น · มีออเดอร์ค้าง 100 ตัว"""
import asyncio
import pytest
from rbsi_bot.grid import ZoneConfig, Snapshot, Order, plan, expected, window_ranks, WINDOW_SLACK
from tests.fake_exchange import FakeExchange
from tests.test_bot_sim import scenario, make, check, settle

ZH = ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10, direction="long")          # HFT-1 (max_orders ค่าเริ่ม 90)
ZS = ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10)                            # short กรอบเดียวกัน


def orders(z, m, ks=None):
    return [Order(f"o{k}", side, px, 10, 0, z.label(k), side == z.close_side) for k, (side, px) in expected(z, m).items() if ks is None or k in ks]


def snap(z, m, mark, os_):
    pos = (1 if z.is_long else -1) * m * 10
    return Snapshot(position=pos, mark=mark, best_bid=mark - 0.5, best_ask=mark + 0.5, orders=os_, ledger={k: 10.0 for k in range(1, m + 1)})


def nearest(z, m, mark, count):
    exp = expected(z, m)
    return sorted(exp, key=lambda k: (abs(exp[k][1] - mark), exp[k][1]))[:count]


def test_config_limits():
    assert ZH.max_orders == 90 and ZH.n == 180
    assert ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10, max_orders=0).max_orders == 0          # 0 = ไม่จำกัด (แบบเดิม)
    for bad in (3, 95, 200):
        with pytest.raises(ValueError):
            ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10, max_orders=bad)


def test_fresh_places_only_nearest_90():
    d = plan(ZH, snap(ZH, 119, 83048, []))
    lim = [a for a in d.actions if a[0] == "limit"]
    assert d.status == "ok" and len(lim) == 90 and d.info["want_orders"] == 90
    assert {a[1] for a in lim} == set(nearest(ZH, 119, 83048, 90))
    sells = [a for a in lim if a[2] == "sell"]; buys = [a for a in lim if a[2] == "buy"]
    assert all(a[5] for a in sells) and not any(a[5] for a in buys)                  # long: sell คืน reduce-only · buy เปิด
    assert max(a[3] for a in buys) < 83048 < min(a[3] for a in sells)
    assert len(sells) in (44, 45, 46) and len(buys) in (44, 45, 46)                  # ฝั่งละราว 45 ชั้น (±4,500)


def test_live_hft1_state_trims_to_window():
    """จอจริง: 100 ออเดอร์ใกล้ราคาค้างอยู่ → ยกเลิกเฉพาะ 4 ตัวที่ไกลสุด (อันดับ 97–100) ไม่วางเพิ่ม"""
    near100 = nearest(ZH, 119, 83048, 100)
    d = plan(ZH, snap(ZH, 119, 83048, orders(ZH, 119, set(near100))))
    assert d.status == "ok" and all(a[0] == "cancel" for a in d.actions) and len(d.actions) == 4
    assert {a[2] for a in d.actions} == set(near100[96:])


def test_wiggle_inside_slack_no_churn():
    """มี 90 ตัวใกล้สุดอยู่แล้ว ราคาแกว่งระหว่างชั้น 83,005–83,095 (ไม่แตะชั้นใหม่) → อันดับขอบหน้าต่างสลับได้แต่ไม่ยกเลิก (อยู่ใน slack)"""
    near90 = set(nearest(ZH, 119, 83048, 90))
    for p in (83005, 83048, 83095):
        d = plan(ZH, snap(ZH, 119, p, orders(ZH, 119, near90)))
        assert d.status == "ok"
        assert not [a for a in d.actions if a[0] == "cancel"], (p, d.actions)
        assert len([a for a in d.actions if a[0] == "limit"]) <= 3


def test_window_follows_price_and_never_exceeds_cap():
    """ราคาขยับไกล (ชั้นถือเปลี่ยนตามจริง) → ยกเลิกตัวไกล วางตัวใกล้ จำนวนรวมหลังลงมือ ≤ max_orders + slack"""
    os_ = orders(ZH, 119, set(nearest(ZH, 119, 83048, 90)))
    for m, p in ((109, 84040), (99, 85030), (135, 81450), (150, 79960)):
        # ราคาวิ่งมาแบบที่ fill ครบตามสูตรแล้ว: บัญชีไม้ = m ชั้น · ออเดอร์เดิมของชั้นที่ฝั่งเปลี่ยนถือว่าหายไปแล้ว (fill)
        exp = expected(ZH, m)
        os_ = [o for o in os_ if exp[int(o.label[-3:])][0] == o.side]
        d = plan(ZH, snap(ZH, m, p, os_))
        assert d.status == "ok", d.reason
        gone = {a[1] for a in d.actions if a[0] == "cancel"}
        os_ = [o for o in os_ if o.order_id not in gone] + [Order(f"n{a[1]}{a[2]}", a[2], a[3], a[4], 0, ZH.label(a[1]), a[5]) for a in d.actions if a[0] == "limit"]
        assert len(os_) <= ZH.max_orders + WINDOW_SLACK
        have = {int(o.label[-3:]) for o in os_}
        assert set(nearest(ZH, m, p, 90)) <= have                                     # ใกล้ราคา 90 ตัวมีครบ


def test_late_detection_still_works_beyond_window():
    """ราคากระโดดลงทะลุหน้าต่าง (ไม่มีออเดอร์ในชั้นนั้น) → ชั้นที่ราคาผ่านไปแล้วนับเป็น late → halt ตามเดิม ไม่เงียบ"""
    d = plan(ZH, snap(ZH, 119, 77500, orders(ZH, 119, set(nearest(ZH, 119, 83048, 90)))))
    assert d.status == "halt" and "ทิ้งห่าง" in d.reason


def test_small_zone_unchanged():
    """zone ที่ออเดอร์รวม ≤ max_orders ทำงานแบบเดิมทุกอย่าง (วางครบทุกชั้น)"""
    z = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000, direction="long")
    d = plan(z, Snapshot(position=9000, mark=83458, best_bid=83457.5, best_ask=83458.5, orders=[], ledger={k: 1000.0 for k in range(1, 10)}))
    assert len([a for a in d.actions if a[0] == "limit"]) == 12 and d.info["want_orders"] == 12


def test_ranks_deterministic_on_ties():
    req = {1: {"sell": (83100.0, 10)}, 2: {"buy": (82900.0, 10)}}
    assert window_ranks(req, 83000.0) == {(2, "buy"): 0, (1, "sell"): 1}           # ห่างเท่ากัน → ราคาต่ำก่อน


# ---------- จำลองทั้งระบบบน exchange ที่มีเพดาน 100 ออเดอร์จริง ----------
@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("z,m0", [(ZH, 116), (ZS, 65)], ids=["long", "short"])
def test_random_walk_180_levels_cap_100(tmp_path, seed, z, m0):
    ex, bot, store = asyncio.run(scenario(tmp_path, 900 + seed, pos_lag=2, trade_lag=3, timeout_prob=0.1, partial_prob=0.2,
                                          manual_orders=False, z=z, m0=m0))
    assert ex.max_open_rejects == 0, f"โดน Deribit ปฏิเสธเพราะเกิน 100 ออเดอร์ {ex.max_open_rejects} ครั้ง"
    assert ex.max_open_seen <= z.max_orders + WINDOW_SLACK


@pytest.mark.parametrize("seed", range(2))
def test_random_walk_180_levels_with_jumps(tmp_path, seed):
    ex, bot, store = asyncio.run(scenario(tmp_path, 920 + seed, pos_lag=1, trade_lag=2, partial_prob=0.2, jump=0.08,
                                          manual_orders=False, z=ZH, m0=116))
    assert ex.max_open_rejects == 0 and ex.max_open_seen <= ZH.max_orders + WINDOW_SLACK


def test_restart_with_100_existing_orders_converges(tmp_path):
    """แบบ HFT-1 ตอนนี้: เริ่มบอทใหม่บน zone ที่มีออเดอร์ของบอท 100 ตัวค้าง → ตัดเหลือ ≤ 96 แล้ววางตามหน้าต่าง ไม่มีถูกปฏิเสธ"""
    async def run():
        ex = FakeExchange(83048)
        ex.pos = 1190.0; ex.inv = 1190 / 82546.3; ex.pos_hist = {0: ex.pos}
        bot, store = make(tmp_path, ex, z=ZH)
        await bot.adopt()
        # จำลองบอทเวอร์ชันเก่าที่วางไว้ 100 ตัวใกล้สุด (ติด label ของบอทแล้ว)
        exp = expected(ZH, 119)
        for k in nearest(ZH, 119, 83048, 100):
            await ex.place(exp[k][0], "BTC-PERPETUAL", 10, price=exp[k][1], label=ZH.label(k), reduce_only=exp[k][0] == "sell")
        ex.advance(1)
        await settle(bot, ex, 20, jitter=30)
        own = ex.own_open(ZH.zone_id)
        assert store.data["status"] == "active", store.data["halt_reason"]
        assert ZH.max_orders <= len(own) <= ZH.max_orders + WINDOW_SLACK and ex.max_open_rejects == 0
    asyncio.run(run())
