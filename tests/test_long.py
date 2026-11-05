# -*- coding: utf-8 -*-
"""zone ทิศ long (Long Bias): กระจกเงาของ short — position = 0 ที่กรอบบน · long เต็มที่กรอบล่าง
ชั้น k: buy (เปิด) ที่ upper − k·gap · sell คืน (reduce-only) ที่ upper − (k−1)·gap
ใช้ด่านกัน double / ledger / post-only ชุดเดียวกับ short + ชุดจำลองสุ่มเดียวกัน (helper ใน test_bot_sim)"""
import asyncio, json, os, re, shutil, subprocess
import pytest
from rbsi_bot.grid import ZoneConfig, Snapshot, Order, plan, expected, m_range, parse_label
from rbsi_bot.liq import liq_long_after_fills, mm_rate, eq_eff
from rbsi_bot.bot import GridBot, Halt
from rbsi_bot.store import Store
from tests.fake_exchange import FakeExchange
from tests.test_bot_sim import ZL, Z, seed_manual, make, check, settle, scenario, SlowTG

MON = os.path.expanduser("~/deribit-monitor/index.html")


def orders_for(m, skip=(), extra=()):
    out = []
    for k, (side, px) in expected(ZL, m).items():
        if k not in skip:
            out.append(Order(f"o{k}", side, px, 1000, 0, ZL.label(k), side == "sell"))
    return out + list(extra)


def snap(m, mark, pos=None, ledger=None, **kw):
    return Snapshot(position=(m * 1000) if pos is None else pos, mark=mark, best_bid=mark - 0.5, best_ask=mark + 0.5,
                    orders=kw.pop("orders", orders_for(m)), ledger={k: 1000.0 for k in range(1, m + 1)} if ledger is None else ledger, **kw)


# ---------- ตรรกะกริด ----------
def test_config_mirror():
    assert ZL.n == 12 and ZL.cap == 12000 and ZL.open_side == "buy" and ZL.close_side == "sell"
    assert ZL.zone_id == "BTC82000-88000g500q1000L" and Z.zone_id == "BTC82000-88000g500q1000"   # short คงชื่อเดิม (ไฟล์สถานะเดิม)
    assert (ZL.buy_price(1), ZL.sell_price(1)) == (87500, 88000)
    assert (ZL.buy_price(12), ZL.sell_price(12)) == (82000, 82500)
    assert parse_label(ZL.label(3), ZL.zone_id) == ("own", 3)
    assert parse_label(ZL.label(3), Z.zone_id)[0] == "other"          # label ของ long ไม่ชนกับ short กรอบเดียวกัน
    with pytest.raises(ValueError):
        ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000, direction="up")


def test_m_range_mirror():
    assert m_range(ZL, 83458) == (8, 10)          # buy 87,500 … 83,500 ผ่านแล้ว 9 ชั้น
    assert m_range(ZL, 89000) == (0, 0)           # เหนือกรอบบน → position 0
    assert m_range(ZL, 81000) == (12, 12)         # ใต้กรอบล่าง → long เต็ม


def test_steady_state_needs_nothing():
    d = plan(ZL, snap(9, 83458))
    assert d.status == "ok" and d.actions == [] and d.m == 9
    assert sorted(o.price for o in orders_for(9) if o.side == "buy") == [82000, 82500, 83000]
    assert all(o.reduce_only for o in orders_for(9) if o.side == "sell")


def test_buy_filled_places_sell_not_buy_again():
    """กัน double ฝั่ง long: buy 83,000 (ชั้น 10) fill → วาง sell 83,500 reduce-only เท่านั้น ไม่วาง buy ซ้ำ"""
    led = {k: 1000.0 for k in range(1, 11)}
    d = plan(ZL, snap(10, 82990, ledger=led, orders=orders_for(9, skip=(10,))))
    assert d.status == "ok" and d.actions == [("limit", 10, "sell", 83500, 1000.0, True)]


def test_stale_position_is_flux():
    led = {k: 1000.0 for k in range(1, 11)}
    d = plan(ZL, snap(9, 82990, ledger=led, orders=orders_for(9, skip=(10,))))
    assert d.status == "flux" and d.actions == []


def test_duplicate_buy_halts():
    dup = Order("dup", "buy", 82500, 1000, 0, ZL.label(11))
    d = plan(ZL, snap(12, 81000, ledger={k: 1000.0 for k in range(1, 13)}, orders=orders_for(12) + [dup]))
    assert d.status == "halt" and "เพดาน" in d.reason


def test_zone_full_only_sells_and_above_upper_only_buys():
    d = plan(ZL, snap(12, 81000))
    assert d.status == "ok" and d.m == 12 and d.actions == [] and all(o.side == "sell" for o in orders_for(12))
    d = plan(ZL, snap(0, 89000, ledger={}))
    assert d.status == "ok" and d.m == 0 and d.actions == [] and all(o.side == "buy" for o in orders_for(0))


def test_above_upper_still_long_sells_at_ask():
    """ราคาทะลุกรอบบนแต่ชั้น 1 ยังถือ (sell หาย) → sell คืนที่ ask (post-only) ไม่ใช่ market"""
    d = plan(ZL, snap(1, 88900, ledger={1: 1000.0}, orders=orders_for(1, skip=(1,))))
    assert d.status == "ok" and d.actions == [("limit", 1, "sell", 88900.5, 1000.0, True)]


def test_guards():
    assert plan(ZL, snap(0, 83458, pos=-1000, ledger={})).status == "halt"        # short ใน zone long
    assert plan(Z, Snapshot(position=1000, mark=83458, best_bid=83457.5, best_ask=83458.5, orders=[], ledger={})).status == "halt"


def test_missing_orders_all_placed_open_not_reduce_only():
    d = plan(ZL, snap(9, 83458, orders=[]))
    assert d.status == "ok" and len(d.actions) == 12
    assert all(a[5] is False for a in d.actions if a[2] == "buy") and all(a[5] is True for a in d.actions if a[2] == "sell")


# ---------- liq long inverse ----------
def bisect_liq_long(wb, legs, r):
    """แก้ wb + Σ q(1/pe − 1/P) = r·ΣQ/P ตรง ๆ (legs = [(entry, q)]) — liq อยู่ใต้ entry"""
    N = sum(q for _, q in legs)
    f = lambda P: wb + sum(q * (1 / pe - 1 / P) for pe, q in legs) - r * N / P
    lo, hi = 1.0, max(pe for pe, _ in legs) * 2
    for _ in range(300):
        m = (lo + hi) / 2
        lo, hi = (lo, m) if f(m) > 0 else (m, hi)
    return (lo + hi) / 2


def test_liq_long_matches_bisection():
    L, E, M, eq, r = 9000.0, 85432.1, 83458.0, 0.004, 0.0126
    adds = [(83000.0, 1000.0), (82500.0, 1000.0), (82000.0, 1000.0)]
    wb = eq - L * (1 / E - 1 / M)
    got = liq_long_after_fills(L, E, M, eq, r, adds)
    assert got == pytest.approx(bisect_liq_long(wb, [(E, L)] + adds, r), rel=1e-9)
    assert got < 82000                                                    # เงินพอ: liq อยู่ใต้กรอบล่าง


@pytest.mark.skipif(not (os.path.exists(MON) and shutil.which("node")), reason="ไม่มี deribit-monitor หรือ node")
def test_liq_long_matches_monitor_js():
    src = open(MON, encoding="utf-8").read()
    fn = re.search(r"function deribitLiqAfterFills\(.*?\n}\n", src, re.S).group(0)
    p = {"size": 9000, "mark_price": 83458.0, "average_price": 85432.1, "direction": "buy", "maintenance_margin": 0.0014, "instrument_name": "BTC-PERPETUAL"}
    sums = [{"equity": 0.004, "maintenance_margin": 0.0016}]
    orders = [{"instrument_name": "BTC-PERPETUAL", "direction": "buy", "price": 83000 - 500 * i, "amount": 1000, "filled_amount": 0} for i in range(3)]
    js = fn + f"\nconsole.log(JSON.stringify(deribitLiqAfterFills({json.dumps(p)},'inverse',{json.dumps(sums)},{json.dumps(orders)},0)))"
    want = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    r = mm_rate(0.0014, 83458.0, 9000.0, 0.0126)
    eq = eq_eff(0.004, 0.0016, 0.0014)
    assert liq_long_after_fills(9000.0, 85432.1, 83458.0, eq, r, [(o["price"], 1000.0) for o in orders]) == pytest.approx(want, rel=1e-12)


# ---------- จำลองทั้งระบบ (ชุดสุ่มเดียวกับ short) ----------
@pytest.mark.parametrize("seed", range(4))
def test_long_random_walk_no_lag(tmp_path, seed):
    asyncio.run(scenario(tmp_path, 700 + seed, 0, 0, z=ZL))


@pytest.mark.parametrize("seed", range(4))
def test_long_random_walk_lagged(tmp_path, seed):
    asyncio.run(scenario(tmp_path, 710 + seed, pos_lag=3, trade_lag=5, z=ZL))


@pytest.mark.parametrize("seed", range(3))
def test_long_partial_fills_and_lost_responses(tmp_path, seed):
    asyncio.run(scenario(tmp_path, 720 + seed, pos_lag=2, trade_lag=4, timeout_prob=0.15, partial_prob=0.3, z=ZL))


@pytest.mark.parametrize("seed", range(3))
def test_long_jumps_stale_orders_purged_history(tmp_path, seed):
    asyncio.run(scenario(tmp_path, 730 + seed, pos_lag=1, trade_lag=3, timeout_prob=0.1, partial_prob=0.2, jump=0.08,
                         order_lag=2, purge_cancelled=True, z=ZL))


@pytest.mark.parametrize("seed", range(2))
def test_long_rate_limited_converges(tmp_path, seed):
    ex, bot, store = asyncio.run(scenario(tmp_path, 740 + seed, pos_lag=2, trade_lag=3, timeout_prob=0.1, rate_limit=2,
                                          manual_orders=False, z=ZL))
    assert ex.throttled > 0


def test_long_manual_trade_halts_cancels_buys_keeps_sells(tmp_path):
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 9, 83458, z=ZL)
        bot, store = make(tmp_path, ex, z=ZL)
        await bot.adopt()
        await settle(bot, ex, 5)
        await ex.place("buy", "BTC-PERPETUAL", 1000, price=None, label="")          # มีคนกด buy มือ
        ex.advance(1)
        await bot.reconcile()
        assert store.data["status"] == "halted"
        own = ex.own_open(ZL.zone_id, store.data["adopted"])
        assert own and all(o["direction"] == "sell" for o in own)                 # buy (เปิด) ถูกยกเลิก sell คืนยังอยู่
    asyncio.run(run())


def test_long_zone_full_then_user_closes(tmp_path):
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 9, 83458, z=ZL)
        bot, store = make(tmp_path, ex, z=ZL)
        await bot.adopt()
        await settle(bot, ex, 5)
        for p in range(83250, 81200, -250):                                       # วิ่งลงจน zone เต็ม
            ex.move_price(p)
            await bot.reconcile()
            check(ex, bot)
            ex.advance(2)
        await settle(bot, ex, 5)
        assert store.data["seen_full"] and round(ex.pos) == ZL.cap
        assert all(o["direction"] == "sell" for o in ex.own_open(ZL.zone_id, store.data["adopted"]))   # หยุดเพิ่ม long คงไว้แต่ sell
        for o in list(ex.own_open(ZL.zone_id, store.data["adopted"])):
            await ex.cancel(o["order_id"])
        await ex.place("sell", "BTC-PERPETUAL", ZL.cap, price=None, label="", reduce_only=True)          # ผู้ใช้ปิดเองเพื่อต่อพอร์ต
        ex.move_price(81300)
        await settle(bot, ex, 3)
        assert store.data["status"] == "closed" and ex.pos == 0
    asyncio.run(run())


def test_long_pair_message(tmp_path):
    """คู่ long: buy 83,100 → sell 83,200 · กำไร = q(1/83,100 − 1/83,200) ขั้นต้น และสุทธิหลัง fee 2 ขา"""
    async def run():
        z = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 100, 200, direction="long")
        store = Store(str(tmp_path), z.zone_id)
        tg = SlowTG(0)
        bot = GridBot(z, FakeExchange(83150), store, tg, live=False)
        buy = {"trade_id": "B1", "order_id": "o1", "timestamp": 1, "direction": "buy", "amount": 200.0, "price": 83100.0, "fee": 0.00000036}
        sell = {"trade_id": "S1", "order_id": "o2", "timestamp": 2, "direction": "sell", "amount": 200.0, "price": 83200.0, "fee": 0.00000036}
        store.add_trade(buy, 49, "buy")                                          # ledger ฝั่ง long: buy = เพิ่มยอดถือ
        assert store.nets()[49] == 200.0
        store.add_trade(sell, 49, "buy")
        assert store.nets()[49] == 0.0
        await bot._notify_fill(sell, 49, {"position": 1000.0})
        await tg.flush()
        gross = 200 * (1 / 83100 - 1 / 83200)
        net = gross - 0.00000072
        assert gross > 0 and f"คู่นี้ {gross:+.7f} BTC" in tg.sent[0] and f"สุทธิหลัง fee {net:+.7f} BTC" in tg.sent[0]
    asyncio.run(run())


def test_long_preflight_liq_below_lower(tmp_path):
    """long: liq ตอน zone เต็มต้องอยู่ใต้ lower − buffer · เงินบาง = ไม่ให้เริ่ม"""
    async def run(equity):
        ex = FakeExchange(83458, equity=equity)
        seed_manual(ex, 9, 83458, z=ZL)
        bot, store = make(tmp_path / str(equity), ex, z=ZL)
        r, _ = await bot.adopt()
        try:
            return await bot.preflight(r)
        except Halt as e:
            return e
    ok = asyncio.run(run(0.02))
    assert isinstance(ok, str) and "liq ถ้า zone เต็ม" in ok
    liq = float(ok.rsplit("≈", 1)[1].replace(",", ""))
    assert liq < 82000 - 500
    bad = asyncio.run(run(0.0005))
    assert isinstance(bad, Halt) and "สูงกว่า lower − buffer" in str(bad)
