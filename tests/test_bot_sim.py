# -*- coding: utf-8 -*-
"""จำลองบอททั้งระบบบน FakeExchange — เส้นทางราคาสุ่ม + ข้อมูล position/fill มาช้า + partial fill + คำตอบหาย
ตรวจทุกก้าว (จากสถานะจริงของ exchange ไม่ใช่จากที่บอทเห็น):
  • ไม่เคยถือผิดฝั่ง · ที่ถือไม่เคยเกินเพดาน zone
  • ที่ถือ + ออเดอร์เปิดที่รอ ไม่เคยเกินเพดาน (ถ้าเกิน = มีการวางซ้ำ/double)
  • แต่ละชั้นแต่ละฝั่งมีออเดอร์ของบอทไม่เกิน 1 ตัว (ชั้นที่ fill บางส่วนมีทั้ง buy และ sell ได้)
  • จบแล้วนิ่ง: |position| = m×size, ออเดอร์ครบตามสูตร, m สอดคล้องกับราคา
helper ทุกตัวรับ zone ได้ทั้ง short (Z) และ long (ZL) — ชุดสุ่มของ long อยู่ใน test_long.py"""
import asyncio, random
import pytest
from rbsi_bot.grid import ZoneConfig, expected, m_range, window_ranks, WINDOW_SLACK
from rbsi_bot.bot import GridBot, Safety
from rbsi_bot.store import Store
from rbsi_bot.notify import Notifier
from rbsi_bot.deribit import RpcError
from tests.fake_exchange import FakeExchange

Z = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000)
ZL = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000, direction="long")


def seed_manual(ex, m, mark, orders=True, z=Z):
    """สถานะเริ่มแบบจอจริง: ถือ m ชั้น + ออเดอร์มือ (ไม่มี label) ครบ 12 ตัว"""
    ex.pos = (1 if z.is_long else -1) * m * float(z.size)
    ex.inv = sum(float(z.size) / z.open_price(k) for k in range(1, m + 1))
    ex.pos_hist = {0: ex.pos}
    for k, (side, px) in (expected(z, m).items() if orders else ()):
        oid = f"M{k}"
        o = {"order_id": oid, "direction": side, "amount": float(z.size), "filled_amount": 0.0, "label": "", "reduce_only": False,
             "price": px, "order_state": "open"}
        ex.orders[oid] = ex.states[oid] = o
    ex.orders_hist = {0: {k: dict(v) for k, v in ex.orders.items()}}       # สถานะตั้งต้นของรายการออเดอร์ที่มาช้า (order_lag)


def make(tmp_path, ex, z=Z, **sf):
    store = Store(str(tmp_path), z.zone_id)
    safety = Safety(settle_sec=0.3, adopt_manual=True, **sf)          # ค่าเดียวกับของจริง
    bot = GridBot(z, ex, store, Notifier(None, None), live=True, safety=safety, sleep=ex.sleep, now=ex.now)
    return bot, store


def check(ex, bot=None, z=None):
    z = z or (bot.cfg if bot else Z)
    own = ex.own_open(z.zone_id, bot.store.data.get("adopted", {}) if bot else ())
    S = z.held(ex.pos)
    assert S >= -1e-9, f"ถือผิดฝั่ง! {ex.pos} (zone {z.word})"
    assert S <= z.cap + 1e-9, f"{z.word} เกินเพดาน {S}"
    opens = sum(o["amount"] - o["filled_amount"] for o in own if o["direction"] == z.open_side)
    assert S + opens <= z.cap + 1e-9, f"double! {z.word} {S} + {z.open_side} รอ {opens} > {z.cap}"
    lv = lambda o: o["label"] or (bot.store.data["adopted"].get(o["order_id"]) if bot else o["order_id"])
    keys = [(lv(o), o["direction"]) for o in own]
    assert len(keys) == len(set(keys)), f"ออเดอร์ซ้ำในชั้น/ฝั่งเดียวกัน {sorted(keys)}"


async def settle(bot, ex, rounds=40, jitter=0.0, seed=0):
    """ให้บอทเก็บงานจนนิ่ง · jitter = ราคาแกว่งเล็กน้อยรอบราคาเดิม (ออเดอร์ post-only ที่ bid/ask ต้องให้ราคาเดินมาหาถึงจะ fill)"""
    rng, base = random.Random(seed), ex.price
    for i in range(rounds):
        if jitter and i < rounds - 8:
            ex.move_price(base + rng.uniform(-jitter, jitter))
        await bot.reconcile()
        check(ex, bot)
        ex.advance(2)


def assert_clean(bot, ex):
    """นิ่งแล้วต้องสะอาด: ที่ถือ = Σ ยอดรายชั้น · ทุกชั้นมีออเดอร์ตรงยอดที่ขาด · ไม่มีชั้นที่ราคาวิ่งเลยโดยไม่จัดการ"""
    Z = bot.cfg
    nets = bot.store.nets()
    assert abs(Z.held(ex.pos) - sum(nets.values())) < 1e-9, (ex.pos, nets)
    tol = Z.tol_frac * Z.gap
    untouched = lambda side, px: ex.price < px + tol if side == "sell" else ex.price > px - tol   # ราคายังไม่วิ่งเลยออเดอร์นี้
    want = []
    for k in range(1, Z.n + 1):
        net = nets.get(k, 0.0)
        if Z.size - net > 1e-9:
            want.append((k, Z.open_side, Z.open_price(k), Z.size - net))
            assert untouched(Z.open_side, Z.open_price(k)), f"ชั้น {k} ควรถูกเปิด {Z.word} แล้ว"
        if net > 1e-9:
            want.append((k, Z.close_side, Z.close_price(k), net))
            assert untouched(Z.close_side, Z.close_price(k)), f"ชั้น {k} ควรถูกปิดแล้ว"
    bid, ask = ex.price - 0.5, ex.price + 0.5
    chase = Z.chase_frac * Z.gap

    def norm(k, side, px):
        """ราคาชั้นทะลุ book แล้ว → ยอมรับออเดอร์ไล่ราคาที่ไม่แย่กว่าราคาชั้นและอยู่ใกล้ bid/ask"""
        lvl = Z.price(k, side)
        if side == "buy" and lvl >= ask:
            assert px <= lvl and abs(px - bid) <= chase + 1e-9, (k, side, px, bid)
            return lvl
        if side == "sell" and lvl <= bid:
            assert px >= lvl and abs(px - ask) <= chase + 1e-9, (k, side, px, ask)
            return lvl
        return px
    adopted = bot.store.data.get("adopted", {})
    lvl = lambda o: int(o["label"][-3:]) if o["label"] else adopted[o["order_id"]]
    got = sorted((lvl(o), o["direction"], norm(lvl(o), o["direction"], o["price"]), o["amount"] - o["filled_amount"])
                 for o in ex.own_open(Z.zone_id, adopted))
    N = Z.max_orders if Z.max_orders and len(want) > Z.max_orders else 0
    if N:                                        # หน้าต่างออเดอร์: ต้องมีครบอันดับ < N · ห้ามมีอันดับ ≥ N + slack · ที่มีต้องตรงสูตร
        rank = window_ranks({k: {sd: (px, amt) for kk, sd, px, amt in want if kk == k} for k in {w[0] for w in want}}, ex.price)
        assert set(got) <= set(want), sorted(set(got) - set(want))
        have = {(k, sd) for k, sd, _, _ in got}
        assert all((k, sd) in have for (k, sd), r in rank.items() if r < N), "ขาดออเดอร์ในหน้าต่าง"
        assert all(rank[(k, sd)] < N + WINDOW_SLACK for k, sd in have), "มีออเดอร์นอกหน้าต่าง"
    else:
        assert got == sorted(want)
    assert all(o["price"] != "market_price" for o in ex.states.values() if (o.get("label") or "").startswith("rbsi_")), "มี market order"
    zero_side, full_side = (ex.price >= Z.upper + tol, ex.price <= Z.lower - tol) if Z.is_long else (ex.price <= Z.lower - tol, ex.price >= Z.upper + tol)
    if zero_side:
        assert ex.pos == 0, "ราคาเลยกรอบฝั่ง position = 0 แล้ว position ต้องเป็น 0"
    if full_side:
        assert Z.held(ex.pos) == Z.cap, "ราคาเลยกรอบฝั่ง zone เต็มแล้ว position ต้องเต็ม"


async def scenario(tmp_path, seed, pos_lag, trade_lag, timeout_prob=0.0, partial_prob=0.0, steps=400, jump=0.0, rate_limit=0, manual_orders=True,
                   order_lag=0, purge_cancelled=False, z=Z, m0=None):
    rng = random.Random(seed)
    ex = FakeExchange(83458, pos_lag=pos_lag, trade_lag=trade_lag, timeout_prob=timeout_prob, partial_prob=partial_prob, seed=seed, rate_limit=rate_limit,
                      order_lag=order_lag, purge_cancelled=purge_cancelled)
    if m0 is None:
        m0 = 3 if not z.is_long else 9                           # 83,458: short 3 ชั้นเหนือ 82k · long 9 ชั้นใต้ 88k
    seed_manual(ex, m0, 83458, orders=manual_orders, z=z)
    bot, store = make(tmp_path, ex, z=z)
    await bot.adopt()
    await settle(bot, ex, 10)
    assert store.data["status"] == "active", store.data["halt_reason"]
    vol = z.gap / 500                                               # ความผันผวนต่อก้าวเทียบเป็นจำนวนชั้น (gap 500 = เส้นทางเดิม)
    for _ in range(steps):
        d = rng.gauss(0, 180 * vol)
        if jump and rng.random() < jump:
            d = rng.choice([-1, 1]) * rng.uniform(500, 900) * vol    # กระโดดทีละ 1–2 ชั้น
        ex.move_price(min(90500, max(80500, ex.price + d)))
        check(ex, bot)
        for _ in range(rng.randint(0, 3)):
            await bot.reconcile()
            check(ex, bot)
            ex.advance(rng.uniform(0.5, 3))
        assert store.data["status"] == "active", store.data["halt_reason"]
    await settle(bot, ex, rounds=60, jitter=40, seed=seed)
    assert store.data["status"] in ("active",), store.data["halt_reason"]
    assert_clean(bot, ex)
    return ex, bot, store


@pytest.mark.parametrize("seed", range(6))
def test_random_walk_no_lag(tmp_path, seed):
    asyncio.run(scenario(tmp_path, seed, 0, 0))


@pytest.mark.parametrize("seed", range(6))
def test_random_walk_lagged_position_and_fills(tmp_path, seed):
    """position ช้า 3 ขั้น fill ช้า 5 ขั้น แต่ออเดอร์หายทันที — สถานการณ์ที่เสี่ยงวาง sell ซ้ำที่สุด"""
    asyncio.run(scenario(tmp_path, 100 + seed, pos_lag=3, trade_lag=5))


@pytest.mark.parametrize("seed", range(4))
def test_partial_fills_and_lost_responses(tmp_path, seed):
    asyncio.run(scenario(tmp_path, 200 + seed, pos_lag=2, trade_lag=3, timeout_prob=0.15, partial_prob=0.3))


@pytest.mark.parametrize("seed", range(4))
def test_gap_jumps_use_taker(tmp_path, seed):
    ex, bot, store = asyncio.run(scenario(tmp_path, 300 + seed, pos_lag=1, trade_lag=2, jump=0.08))


@pytest.mark.parametrize("seed", range(4))
def test_rate_limited_exchange_converges(tmp_path, seed):
    """traffic หนัก: exchange รับได้ 2 คำสั่ง/วินาที + คำตอบหาย + ข้อมูลช้า → ต้องไม่ double และสุดท้ายออเดอร์ครบ"""
    ex, bot, store = asyncio.run(scenario(tmp_path, 400 + seed, pos_lag=2, trade_lag=3, timeout_prob=0.1, rate_limit=2, manual_orders=False))
    assert ex.throttled > 0                                   # โดนจำกัดจริงระหว่างทดสอบ


def test_manual_trade_halts_and_cancels_sells(tmp_path):
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        await settle(bot, ex, 5)
        # มีคนกด sell มือ 1,000 (ไม่มี label) → บอทต้อง halt ไม่เดาแก้เอง
        await ex.place("sell", "BTC-PERPETUAL", 1000, price=None, label="")
        ex.advance(1)
        await bot.reconcile()
        assert store.data["status"] == "halted"
        assert all(o["direction"] == "buy" for o in ex.own_open(Z.zone_id, bot.store.data["adopted"]))       # sell ถูกยกเลิก buy ยังอยู่
    asyncio.run(run())


def test_handoff_after_full_closes_zone(tmp_path):
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        await settle(bot, ex, 5)
        for p in range(83500, 88600, 250):                                        # วิ่งขึ้นจน zone เต็ม
            ex.move_price(p)
            await bot.reconcile()
            check(ex, bot)
            ex.advance(2)
        await settle(bot, ex, 5)
        assert store.data["seen_full"] and round(-ex.pos) == Z.cap
        # ผู้ใช้ปิด position เองเพื่อต่อพอร์ต (market buy ไม่มี label)
        for o in list(ex.own_open(Z.zone_id, bot.store.data["adopted"])):
            await ex.cancel(o["order_id"])
        await ex.place("buy", "BTC-PERPETUAL", Z.cap, price=None, label="", reduce_only=True)
        ex.move_price(88300)
        await settle(bot, ex, 3)
        assert store.data["status"] == "closed" and ex.pos == 0 and not ex.own_open(Z.zone_id, bot.store.data["adopted"])
    asyncio.run(run())


def test_restart_rebuilds_from_exchange(tmp_path):
    """บอทตายกลางทาง แล้วเปิดใหม่ → อ่านสถานะจาก exchange + ledger ในไฟล์ ไม่วางซ้ำ"""
    async def run():
        ex = FakeExchange(83458, pos_lag=2, trade_lag=3, seed=7)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        rng = random.Random(7)
        for _ in range(150):
            ex.move_price(min(90000, max(81000, ex.price + rng.gauss(0, 200))))
            await bot.reconcile()
            check(ex, bot)
            ex.advance(1)
        store.save()
        bot2, store2 = make(tmp_path, ex)            # โหลดไฟล์สถานะเดิม
        assert store2.data["status"] == "active"
        for _ in range(150):
            ex.move_price(min(90000, max(81000, ex.price + rng.gauss(0, 200))))
            await bot2.reconcile()
            check(ex, bot)
            ex.advance(1)
        await settle(bot2, ex)
        assert store2.data["status"] == "active", store2.data["halt_reason"]
        assert_clean(bot2, ex)
    asyncio.run(run())



def test_adopt_manual_orders_in_place(tmp_path):
    """นับออเดอร์มือที่ตรงสูตรเป็นของบอท (ไม่ยกเลิก/วางใหม่) · ชั้นที่ผู้ใช้ถอดออก บอทวางเพิ่ม · ตัวไม่ตรงสูตรยกเลิก"""
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        await ex.cancel("M2"); await ex.cancel("M7")                           # ผู้ใช้ถอดออกบางส่วน
        ex.orders["X"] = ex.states["X"] = {"order_id": "X", "direction": "sell", "amount": 500.0, "filled_amount": 0.0, "label": "",
                                           "reduce_only": False, "price": 84000.0, "order_state": "open"}   # ขนาดไม่ตรงสูตร
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        assert len(store.data["adopted"]) == 10 and "X" not in store.data["adopted"]
        assert ex.states["X"]["order_state"] == "cancelled" and ex.states["M5"]["order_state"] == "open"
        placed_before = ex.placed
        await settle(bot, ex, 6)
        assert ex.placed - placed_before == 2                                   # วางแค่ชั้น 2 กับ 7 ที่ขาด
        assert_clean(bot, ex)
        # adopted sell ชั้น 4 (84,000) fill → ต้องได้ buy 83,500 ไม่ใช่ sell ซ้ำ
        ex.move_price(84010)
        await settle(bot, ex, 6)
        assert round(-ex.pos) == 4000 and ex.states["M4"]["order_state"] == "filled"
        assert_clean(bot, ex)
    asyncio.run(run())


def test_manual_orders_need_flag(tmp_path):
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        store = Store(str(tmp_path), Z.zone_id)
        bot = GridBot(Z, ex, store, Notifier(None, None), live=True, safety=Safety(settle_sec=1.0), sleep=ex.sleep, now=ex.now)
        with pytest.raises(Exception, match="adopt-manual"):
            await bot.adopt()
    asyncio.run(run())


def test_telegram_status_command(tmp_path):
    """พิมพ์ /status ใน Telegram → บอทตอบสถานะ (ข้อความเก่าที่ค้างอยู่ตอนเริ่มถูกข้าม)"""
    class FakeTG(Notifier):
        def __init__(self):
            super().__init__(None, None)
            self.sent, self.inbox = [], [["/start"], ["/status"], []]
        async def poll(self, timeout=25):
            await asyncio.sleep(0.001)                      # ของจริงรอเครือข่าย — ต้องปล่อย event loop
            return self.inbox.pop(0) if self.inbox else []
        async def send(self, text, key=None):
            self.sent.append(text)

    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        store = Store(str(tmp_path), Z.zone_id)
        tg = FakeTG()
        bot = GridBot(Z, ex, store, tg, live=True, safety=Safety(settle_sec=1.0, adopt_manual=True, summary_hours=0), sleep=ex.sleep, now=ex.now)
        await bot.adopt()
        await bot.reconcile()
        tg.sent.clear()
        task = asyncio.create_task(bot.tg_loop())
        await asyncio.sleep(0.05)
        store.data["status"] = "closed"
        await asyncio.wait_for(task, 1)
        assert len(tg.sent) == 1 and "ทำงานอยู่" in tg.sent[0] and "position -3,000" in tg.sent[0]
        assert "ออเดอร์ของบอท 12/12" in tg.sent[0]
    asyncio.run(run())


def test_heartbeat_log_formats(tmp_path, caplog):
    """log "ทำงานปกติ" ต้อง format ได้จริง (logging กลืน error เงียบ ๆ — ต้องเช็คข้อความที่ออกมา)"""
    import logging
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex, heartbeat_sec=0)
        await bot.adopt()
        with caplog.at_level(logging.INFO, logger="bot"):
            await bot.reconcile()
        msgs = [r.getMessage() for r in caplog.records if "ทำงานปกติ" in r.getMessage()]
        assert msgs and "position -3,000" in msgs[0] and "12/12" in msgs[0]
    asyncio.run(run())


def _down_fill_then_manual_buy(tmp_path):
    """บอทหยุด → ราคาขึ้นจน sell ชั้น 4 (84,000) fill ตอนไม่มีบอท → ผู้ใช้วาง buy คืน 83,500 เอง (ไม่มี label ไม่ reduce-only)
    (เหตุการณ์จริง 28 ก.ย. 2026 23:26 บน Beta2)"""
    async def prep():
        ex = FakeExchange(83458, equity=0.00695)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        await settle(bot, ex, 6)
        store.save()
        ex.move_price(84010)                                     # บอทหยุดอยู่: sell ชั้น 4 fill ไม่มีใคร reconcile
        assert round(-ex.pos) == 4000
        ex.orders["U1"] = ex.states["U1"] = {"order_id": "U1", "direction": "buy", "amount": 1000.0, "filled_amount": 0.0, "label": "",
                                             "reduce_only": False, "price": 83500.0, "order_state": "open"}
        return ex
    return asyncio.run(prep())


def test_restart_counts_fills_while_down_in_preflight(tmp_path):
    """preflight ต้องนับ fill ที่เกิดตอนบอทหยุด — เดิมใช้บัญชีไม้เก่า นับชั้น 4 เป็น sell ที่ยังรอซ้ำ → liq ต่ำกว่าจริง ~480 → ปฏิเสธเริ่มผิด ๆ"""
    ex = _down_fill_then_manual_buy(tmp_path)

    async def run():
        bot, store = make(tmp_path, ex)                           # adopt_manual=True
        r = await bot.read()
        acc = await ex.account_summary()
        stale, _ = bot.projected_liq(r, acc, store.nets())
        truth, _ = bot.projected_liq(r, acc, {k: 1000.0 for k in range(1, 5)})
        assert stale < truth - 300                                 # อาการเดิม
        new, got = await bot.catch_up(r)
        assert [k for _, k in new] == [4] and got == [(4, "buy", 83500.0)]
        assert bot.projected_liq(r, acc, store.nets())[0] == pytest.approx(truth, abs=1e-6)
        placed = ex.placed
        await settle(bot, ex, 6)
        assert store.data["status"] == "active", store.data["halt_reason"]
        assert ex.placed == placed                                 # ไม่วางอะไรเพิ่ม: buy มือถูกนับเป็นชั้น 4
        assert_clean(bot, ex)
        ex.move_price(83490)                                       # buy มือ fill → ชั้น 4 กลับเป็น sell 84,000
        await settle(bot, ex, 6)
        assert round(-ex.pos) == 3000 and store.nets().get(4, 0) == 0
        assert_clean(bot, ex)
    asyncio.run(run())


def test_restart_manual_buy_without_flag_halts_with_hint(tmp_path):
    ex = _down_fill_then_manual_buy(tmp_path)

    async def run():
        store = Store(str(tmp_path), Z.zone_id)
        bot = GridBot(Z, ex, store, Notifier(None, None), live=True, safety=Safety(settle_sec=1.0), sleep=ex.sleep, now=ex.now)
        r = await bot.read()
        new, got = await bot.catch_up(r)
        assert len(new) == 1 and got == []                         # ไม่มี flag = ไม่รับออเดอร์มือ แต่ fill ยังเข้าบัญชีไม้
        await bot.reconcile()
        assert store.data["status"] == "halted" and "--adopt-manual" in store.data["halt_reason"]
    asyncio.run(run())


# ---------- ความเร็ว (29 ก.ย. 2026): Telegram ไม่ถ่วงออเดอร์ · error ตอนอ่านไม่ทำให้ process ตาย ----------
class SlowTG(Notifier):
    """Telegram ช้า 0.5 วิต่อข้อความ (ของจริงวัดได้ 0.6–0.8 วิ บางครั้งหลายวิ)"""
    def __init__(self, delay=0.5):
        super().__init__(None, None)
        self.sent, self.delay = [], delay
    async def send(self, text, key=None):
        await asyncio.sleep(self.delay)
        self.sent.append(text)


def test_slow_telegram_does_not_delay_counter_order(tmp_path, caplog):
    import logging, time as _t
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        store = Store(str(tmp_path), Z.zone_id)
        tg = SlowTG()
        bot = GridBot(Z, ex, store, tg, live=True, safety=Safety(settle_sec=0.3, adopt_manual=True), sleep=ex.sleep, now=ex.now)
        await bot.adopt()
        await settle(bot, ex, 4)
        ex.move_price(84010)                                     # sell ชั้น 4 fill
        placed = ex.placed
        t0 = _t.perf_counter()
        with caplog.at_level(logging.INFO, logger="bot"):
            await bot.reconcile()
        took = _t.perf_counter() - t0
        assert ex.placed == placed + 1 and any(o["price"] == 83500.0 and o["direction"] == "buy" for o in ex.orders.values())
        assert took < 0.2, f"ยิงไม้ฝั่งตรงข้ามช้า {took:.2f} วิ (รอ Telegram อยู่?)"
        assert any("fill→active" in r.getMessage() for r in caplog.records)
        await tg.flush()
        assert any("SELL 84,000.0" in m for m in tg.sent)       # ข้อความ fill ยังส่งถึง (ตามไปทีหลัง)
    asyncio.run(run())


def test_post_keeps_order_and_halt_waits_for_queue():
    async def run():
        tg = SlowTG(0.02)
        t0 = asyncio.get_running_loop().time()
        for i in range(3):
            tg.post(f"m{i}")
        assert asyncio.get_running_loop().time() - t0 < 0.01     # post ไม่รอ
        await tg.flush()
        await tg.send("HALT")
        assert tg.sent == ["m0", "m1", "m2", "HALT"]
    asyncio.run(run())


def _flaky(ex, fail_at, err):
    calls = {"n": 0}
    orig = ex.position
    async def position(ins):
        calls["n"] += 1
        if calls["n"] in fail_at:
            raise RpcError("private/get_position", err)
        return await orig(ins)
    ex.position = position
    return calls


def test_read_rpc_error_does_not_kill_bot(tmp_path):
    """เดิม: Deribit ตอบ error ตอน "อ่าน" (เช่น too_many_requests) → exception หลุดออกจาก run() → process ตาย ออเดอร์ไม่มีคนดูแล"""
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex, reconcile_sec=0.01)
        calls = _flaky(ex, {8, 9}, {"message": "too_many_requests", "code": 10028})
        async def stopper():
            while calls["n"] < 20:
                await asyncio.sleep(0.005)
            ex.move_price(84010)                                  # fill หลังเจอ error แล้ว → บอทยังต้องวาง buy คืน
            while not any(o["price"] == 83500.0 for o in ex.orders.values()):
                await asyncio.sleep(0.005)
            store.data["status"] = "closed"
        res = await asyncio.wait_for(asyncio.gather(bot.run(), stopper()), 10)
        assert res[0] == "closed" and calls["n"] > 20
        check(ex, bot)
    asyncio.run(run())


def test_permanent_error_before_start_refuses_instead_of_looping(tmp_path):
    async def run():
        ex = FakeExchange(83458)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        _flaky(ex, set(range(1, 1000)), {"message": "invalid_credentials", "code": 13004})
        assert await asyncio.wait_for(bot.run(), 5) == "refused"
    asyncio.run(run())


def test_pair_message_shows_net_after_fees(tmp_path):
    """ข้อความคู่ปิด: กำไรขั้นต้น + สุทธิหลัง fee จริงทั้ง 2 ขา (ตัวเลขจากคู่จริง 29 ก.ย. 11:17/11:35 · g100 × 200)"""
    async def run():
        z = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 100, 200)
        store = Store(str(tmp_path), z.zone_id)
        tg = SlowTG(0)
        bot = GridBot(z, FakeExchange(83150), store, tg, live=False)
        sell = {"trade_id": "S1", "order_id": "o1", "timestamp": 1, "direction": "sell", "amount": 200.0, "price": 83200.0, "fee": 0.00000036}
        buy = {"trade_id": "B1", "order_id": "o2", "timestamp": 2, "direction": "buy", "amount": 200.0, "price": 83100.0, "fee": 0.00000036}
        store.add_trade(sell, 12); store.add_trade(buy, 12)
        await bot._notify_fill(buy, 12, {"position": -2200.0})
        await tg.flush()
        gross = 200 * (1 / 83100 - 1 / 83200)
        net = gross - 0.00000072
        assert f"คู่นี้ {gross:+.7f} BTC" in tg.sent[0] and f"สุทธิหลัง fee {net:+.7f} BTC (~${net * 83100:,.2f})" in tg.sent[0]
        assert "(~$0.18)" in tg.sent[0] and "fee ขาย≈ขาซื้อ" not in tg.sent[0]
        # ไม้ sell ที่บอทไม่รู้ fee → ใช้อัตราขาซื้อ และบอกไว้ในข้อความ
        store.data["ledger"]["last_sell_fee"].clear()
        await bot._notify_fill(buy, 12, {"position": -2200.0})
        await tg.flush()
        assert f"สุทธิหลัง fee {net:+.7f}" in tg.sent[1] and "fee ขาย≈ขาซื้อ" in tg.sent[1]
    asyncio.run(run())


# ---------- เหตุการณ์จริง 7 ต.ค. 2026 ----------
def test_filled_buy_still_listed_does_not_halt(tmp_path):
    """position อัปเดตแล้ว แต่ buy ที่เพิ่ง fill ยังค้างในรายการ open orders (มาช้ากว่า)
    เดิม: halt "buy ที่รอ > short" แล้วยกเลิก sell ทั้งหมด · ตอนนี้: รอให้นิ่งแล้วทำงานต่อ"""
    async def run():
        ex = FakeExchange(83458, order_lag=2, purge_cancelled=True)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        await settle(bot, ex, 10)
        ex.move_price(82900)                                   # buy ชั้น 3 (83,000) fill ทันที
        await bot.reconcile()                                  # position ใหม่ + รายการออเดอร์เก่า
        assert store.data["status"] == "active", store.data["halt_reason"]
        await settle(bot, ex, 20)
        assert store.data["status"] == "active", store.data["halt_reason"]
        assert_clean(bot, ex)
    asyncio.run(run())


def test_restart_after_halt_with_purged_cancelled_orders(tmp_path):
    """halt ยกเลิก sell ไปแล้ว Deribit ลบประวัติออเดอร์ที่ยกเลิกแบบไม่มี fill (order_not_found)
    เดิม: เริ่มใหม่ด้วย --clear-halt แล้ววน "รอยืนยันสถานะออเดอร์ที่หายไป" ไม่วางออเดอร์ · ตอนนี้: ปลดล็อกแล้ววางครบ"""
    async def run():
        ex = FakeExchange(83458, purge_cancelled=True)
        seed_manual(ex, 3, 83458)
        bot, store = make(tmp_path, ex)
        await bot.adopt()
        await settle(bot, ex, 10)
        await bot.halt("ทดสอบ")
        assert not [o for o in ex.own_open(Z.zone_id, store.data["adopted"]) if o["direction"] == "sell"]
        store.save()
        bot2, store2 = make(tmp_path, ex)                      # เหมือนกดเริ่มใหม่ด้วย --clear-halt
        store2.data.update(status="active", halt_reason="")
        await settle(bot2, ex, 10)
        assert store2.data["status"] == "active", store2.data["halt_reason"]
        assert_clean(bot2, ex)
    asyncio.run(run())


@pytest.mark.parametrize("seed", range(4))
def test_random_walk_stale_order_list_and_purged_history(tmp_path, seed):
    asyncio.run(scenario(tmp_path, seed, pos_lag=0, trade_lag=1, partial_prob=0.2, order_lag=2, purge_cancelled=True))
