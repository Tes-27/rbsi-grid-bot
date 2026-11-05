# -*- coding: utf-8 -*-
"""โอนออกได้เท่าไร (rbsi_bot/transfer.py) — สูตร IM ของ Deribit เทียบตัวเลขจริงจาก API 20 ต.ค. 2026"""
import pytest
from rbsi_bot.grid import ZoneConfig
from rbsi_bot.transfer import im_account, scan, transferable


def rng(a, b, step):
    return [float(p) for p in range(a, b + 1, step)]


@pytest.mark.parametrize("amt,px,want", [(1000, 60000, 0.00033333), (1000, 90000, 0.00022222), (100000, 77000, 0.02597403), (10, 110000, 0.00000182)])
def test_empty_account_matches_get_margins(amt, px, want):
    """private/get_margins บน sub เปล่า (mark 81,877) = 2% × q/p ทั้ง buy และ sell"""
    assert im_account(0, 81877.3, [(px, amt)], []) == pytest.approx(want, abs=1e-8)
    assert im_account(0, 81877.3, [], [(px, amt)]) == pytest.approx(want, abs=1e-8)


def test_live_snapshot_hft1_long():
    """HFT-1: long +1,310 · buy 77,200–81,800 · sell reduce-only 82,000–86,600 (×10) · Deribit initial_margin 0.00159543"""
    im = im_account(1310, 81854.84, [(p, 10) for p in rng(77200, 81800, 100)], [(p, 10) for p in rng(82000, 86600, 100)])
    assert im == pytest.approx(0.00159543, rel=0.005)


def test_live_snapshot_beta1_short():
    """Beta1: short −27,000 · buy reduce-only 55,000–81,000 · sell 83,000–88,000 (×1,000) · Deribit initial_margin 0.03259871"""
    im = im_account(-27000, 81870.6, [(p, 1000) for p in rng(55000, 81000, 1000)], [(p, 1000) for p in rng(83000, 88000, 1000)])
    assert im == pytest.approx(0.03259871, rel=0.005)


Z = ZoneConfig("BTC-PERPETUAL", 55000, 88000, 1000, 1000, max_orders=0)


def test_available_flat_inside_zone_without_window():
    """ราคาวิ่งในกรอบไม่ทำให้ available ลด (IM รวมขาดทุนระหว่างทางไว้แล้ว) — ไม่มีหน้าต่างออเดอร์ · ไม่รวม funding/fee"""
    rows = [x for x in scan(Z, 81870.6, 0.0332, 0.02) if x["opening"] > 0]
    av = [x["available"] for x in rows]
    assert max(av) - min(av) < 1e-5 and min(av) > 0


def test_index_start_matches_deribit_exactly():
    """เดินทางเริ่มจาก index (ไม่ใช่ mark) → ตรง Deribit < 0.05% (เริ่มจาก mark คลาด −0.37% = |position| × premium)
    Beta1 21 ต.ค.: mark 82,783.84 · index 82,756.99 · Deribit initial_margin 0.02899307"""
    buys, sells = [(p, 1000) for p in rng(55000, 82000, 1000)], [(p, 1000) for p in rng(84000, 88000, 1000)]
    assert im_account(-28000, 82783.84, buys, sells, index=82756.99) == pytest.approx(0.02899307, rel=0.0005)
    assert im_account(-28000, 82783.84, buys, sells) == pytest.approx(0.02899307 * (1 - 0.0037), rel=0.0005)


def test_margin_call_case_beta1_needs_small_topup():
    """เหตุการณ์จริง 20 ต.ค.: Beta1 available 0.0006 — แผงแรกบอกโอนได้เท่า available → โอนแล้ว IM 100% ทันที
    เกณฑ์ IM ≤ 95% บอกให้เติม 0.005 (เกินจริง) · ตอนนี้เผื่อ 1 ไม้ + premium 0.2% → โอนไม่ได้ + เติมไม่ถึง 0.002"""
    base = {"mark": 81870.6, "index": 81870.6, "equity": 0.0332, "position": -27000, "pos_im": 0.0065958}
    t = transferable(Z, {**base, "available": 0.0006, "withdrawable": 0.0006})
    assert t["transferable"] == 0 and 0 < t["topup"] < 0.002
    assert t["layer_btc"] == pytest.approx(0.02 * 1000 / 81870.6, rel=1e-4)                           # r สอบเทียบจาก pos_im
    assert transferable(Z, {**base, "available": -0.001, "withdrawable": 0.0})["topup"] > 0                # IM เกินแล้ว


@pytest.mark.parametrize("zone", [Z, ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10, direction="long", max_orders=90)])
def test_after_transfer_keeps_layer_buffer_everywhere(zone):
    """โอนตามแผงแล้ว ทุกราคาในกรอบที่ยังมีออเดอร์เปิด (premium แกว่งไปทางเสียเปรียบ 0.2%) ยังเหลือ available ≥ IM 1 ไม้"""
    mark, eq = 81870.6, 0.2
    pos = -27000 if not zone.is_long else 1310
    now = next(x for x in scan(zone, mark, eq, 0.02) if x["price"] == mark)
    a = {"mark": mark, "index": mark, "equity": eq, "position": pos, "pos_im": 0.02 * abs(pos) / mark, "available": now["available"], "withdrawable": None}
    t = transferable(zone, a)
    assert t["transferable"] > 0 and t["topup"] == 0
    adv = -0.002 if zone.is_long else 0.002
    for x in scan(zone, mark, eq, 0.02, adv):
        if x["opening"] > 0:
            assert x["available"] - t["transferable"] >= 0.02 * zone.size / x["price"] - 1e-12
    assert transferable(zone, {**a, "withdrawable": 0.0001})["transferable"] == 0.0001                   # ไม่เกินยอดที่ Deribit ให้โอน


def test_premium_already_worse_than_buffer_uses_current():
    """premium ตอนนี้แย่กว่าที่เผื่อไว้ (short + premium +0.5%) → ใช้ค่าจริง โอนได้น้อยลง"""
    mark, eq = 81870.6, 0.2
    now = next(x for x in scan(Z, mark, eq, 0.02) if x["price"] == mark)
    a = {"mark": mark, "equity": eq, "position": -27000, "pos_im": 0.02 * 27000 / mark, "available": now["available"], "withdrawable": None}
    calm = transferable(Z, {**a, "index": mark / 1.001})
    hot = transferable(Z, {**a, "index": mark / 1.005})
    assert hot["transferable"] < calm["transferable"]


def test_window_far_orders_lower_worst_case():
    """หน้าต่างออเดอร์: ราคาเข้าใกล้ชั้นไกล → บอทวางเพิ่ม → available ในกรอบต่ำกว่าตอนนี้ได้"""
    zl = ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10, direction="long", max_orders=90)
    full = ZoneConfig("BTC-PERPETUAL", 77000, 95000, 100, 10, direction="long", max_orders=0)
    a = {"mark": 81854.84, "equity": 0.001875, "position": 1310, "pos_im": 0.00032008, "available": 0.01, "withdrawable": 0.01}
    assert transferable(full, a)["worst_available"] <= transferable(zl, a)["worst_available"] + 1e-12   # วางครบทุกชั้น = กันเงินมากสุด
    assert transferable(zl, a)["transferable"] < 0.01
