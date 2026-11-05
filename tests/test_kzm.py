# -*- coding: utf-8 -*-
"""บอท KZM Grid (spot): ตรรกะล้วน + จำลองทั้งระบบบน FakeSpot (ข้อมูลช้า / fill บางส่วน / คำตอบหาย / กรอบราคา / รีสตาร์ท / โอนเงิน)"""
import asyncio, math, random
import pytest
from rbsi_bot.kzm import KZMConfig, plan, needs, key, parse_key, parse_label, allocate, deallocate, start_plan, cash_to_floor, cash_runs_out, start_cap, full
from rbsi_bot.fv import floor_to
from rbsi_bot.kzmbot import KZMBot, KZMStore, FVSafety, Refuse
from rbsi_bot.grid import Order
from rbsi_bot.notify import Notifier
from tests.fake_spot import FakeSpot

C = KZMConfig(60000, 110000, 500, 5000)            # 100 ชั้น × $50
P0 = 84000.0
A0 = floor_to(50 / (P0 + 0.005), 1e-8)              # BTC ต่อชั้นตั้งต้น (ชั้นละ $50 ซื้อที่ราคาเริ่ม mid = 84,000.005)
FULL = 51 * A0                                       # ชั้นตั้งต้น 49–99 (ชั้น 48 = 84,000 ต่ำกว่า mid → เป็นชั้นรอซื้อ)


# ---------- ตรรกะล้วน ----------
def test_config_and_labels():
    assert C.n == 100 and C.cost == 50 and C.buy_price(0) == 60000 and C.sell_price(0) == 60500 and C.sell_price(99) == 110000
    assert C.fv_id == "KZMBTCUSDC-60000-110000-G500-C5000"
    assert C.amount(48) == round(50 / 84000, 8) or C.amount(48) == pytest.approx(50 / 84000, abs=1e-8)
    kk = key(48, "buy")
    assert kk == "0048b" and parse_key(kk) == (48, "buy") and parse_key("0048s") == (48, "sell") and parse_key("x") is None
    assert parse_label(C, C.label(kk)) == kk and parse_label(C, "fv_x_s") is None and parse_label(C, C.label("zz")) is None
    assert len(C.label(kk)) <= 64                                                  # label ของ Deribit ยาวได้ไม่เกิน 64
    for bad in (dict(ceil=50000), dict(gap=300), dict(capital=100), dict(floor=60000.005), dict(instrument="BTC-PERPETUAL")):
        with pytest.raises(ValueError):
            KZMConfig(**{"floor": 60000, "ceil": 110000, "gap": 500, "capital": 5000, **bad})


def test_start_ratio_buys_layers_above_price_by_chasing():
    d = plan(C, {}, 5000, P0 - 0.01, P0, [])
    places = [a for a in d.actions if a[0] == "place"]
    chase = [a for a in places if a[2] == round(P0 - 0.01, 2)]
    rest = [a for a in places if a not in chase]
    assert [parse_key(a[1]) for a in chase] == [(48, "buy"), (49, "buy"), (50, "buy")]   # ชั้น 84,000 / 84,500 / 85,000 (ทะลุแล้ว ใกล้ราคาก่อน)
    assert [parse_key(a[1])[0] for a in rest] == [47, 46, 45, 44, 43]                    # ชั้นรอใต้ราคา 5 ชั้นใกล้สุด ที่ราคาชั้นพอดี
    assert all(a[2] == C.buy_price(parse_key(a[1])[0]) for a in rest)
    assert not [a for a in places if parse_key(a[1])[1] == "sell"]
    assert d.info["chase_buy"] == 52 and d.info["layers_held"] == 0
    sp = start_plan(C, {}, P0)
    assert sp["layers_ideal"] == 52 and sp["buy_usd"] == pytest.approx(sum(C.amount(k) for k in range(48, 100)) * P0)


def test_partial_layer_has_both_sides_and_amounts_follow_ledger():
    k = 47                                                     # ชั้น 83,500→84,000 ใต้ราคา
    half = round(C.amount(k) / 2, 8)
    w = needs(C, {k: half})[k]
    assert w["sell"] == half and w["buy"] == pytest.approx(C.amount(k) - half, abs=1e-8)
    full = {j: C.amount(j) for j in range(48, 100)}
    d = plan(C, {**full, k: half}, 5000, P0 - 0.01, P0, [])
    acts = {a[1]: a for a in d.actions if a[0] == "place"}
    assert acts[key(k, "sell")][2] == C.sell_price(k) and acts[key(k, "sell")][3] == half
    sells = sorted(parse_key(x)[0] for x in acts if x.endswith("s"))
    assert sells[:2] == [k, 48]                                # ขายชั้นที่ถืออยู่ใกล้ราคาก่อน (ชั้น 48 ขายที่ 84,500)
    d = plan(C, {**full, 40: half}, 5000, P0 - 0.01, P0, [])   # ชั้น 40 ขายที่ 80,500 แต่ราคาอยู่ 84k แล้ว → ขายไล่ราคาที่ bid+tick
    assert next(a for a in d.actions if a[1] == key(40, "sell"))[2] == P0


def test_keep_valid_cancel_wrong_duplicates_and_pending():
    full = {j: C.amount(j) for j in range(48, 100)}
    k = 47
    good = Order("A", "buy", C.buy_price(k), C.amount(k))
    dup = Order("B", "buy", C.buy_price(k), C.amount(k))
    wrong_amt = Order("C", "buy", C.buy_price(46), 0.001)
    wrong_px = Order("D", "sell", C.sell_price(48) + 5, C.amount(48))
    far = Order("E", "buy", C.buy_price(20), C.amount(20))
    own = [(key(k, "buy"), good), (key(k, "buy"), dup), (key(46, "buy"), wrong_amt), (key(48, "sell"), wrong_px), (key(20, "buy"), far)]
    d = plan(C, full, 5000, P0 - 0.01, P0, own)
    cancels = {a[1]: a[3] for a in d.actions if a[0] == "cancel"}
    assert cancels == {"B": "ซ้ำ", "C": "จำนวนไม่ตรงบัญชีไม้", "D": "ราคาไม่ตรงชั้น", "E": "ไกลราคาเกิน"}
    placed = {a[1] for a in d.actions if a[0] == "place"}
    assert key(k, "buy") not in placed and key(46, "buy") not in placed and key(48, "sell") not in placed   # วางใหม่รอบหน้า (หลังยืนยันยกเลิก)
    d = plan(C, full, 5000, P0 - 0.01, P0, [], pending={key(47, "buy")})
    assert key(47, "buy") not in {a[1] for a in d.actions if a[0] == "place"}
    # ออเดอร์ที่ไกลกว่าอันดับ max_orders แต่ไม่เกิน 2 เท่า → คงไว้ (กันยกเลิก/วางใหม่ตอนราคาแกว่งรอบขอบ)
    d = plan(C, full, 5000, P0 - 0.01, P0, [(key(41, "buy"), Order("F", "buy", C.buy_price(41), C.amount(41)))])
    assert not [a for a in d.actions if a[0] == "cancel"]


def test_chase_reprices_and_uncrossed_goes_back_to_level():
    ask = 84200.0
    d = plan(C, {}, 5000, ask - 0.01, ask, [])
    o = next(a for a in d.actions if a[0] == "place" and a[1] == key(49, "buy"))          # ชั้น 84,500 ทะลุแล้ว → ไล่ที่ ask−tick
    assert o[2] == round(ask - 0.01, 2)
    mine = [(key(49, "buy"), Order("A", "buy", o[2], o[3]))]
    d = plan(C, {}, 5000, ask - 0.01 + 1, ask + 1, mine)                                   # ขยับนิดเดียว → คงไว้
    assert not [a for a in d.actions if a[0] == "cancel"]
    d = plan(C, {}, 5000, 84299.99, 84300.0, mine)                                         # ตลาดหนีขึ้น 0.12% แต่ยังใต้ชั้น → ไล่ราคา
    assert ("cancel", "A", key(49, "buy"), "ไล่ราคา") in d.actions
    d = plan(C, {}, 5000, 84599.99, 84600.0, mine)                                         # ราคากลับขึ้นเหนือชั้น → ต้องกลับไปรอที่ราคาชั้น
    assert ("cancel", "A", key(49, "buy"), "ราคาไม่ตรงชั้น") in d.actions


def test_crossed_sells_when_price_jumps_up():
    h = {j: C.amount(j) for j in range(48, 100)}
    bid = 86100.0                                               # ชั้น 48–51 ขายที่ 84.5k–86k ทะลุแล้ว
    d = plan(C, h, 5000, bid, bid + 0.01, [])
    ch = [parse_key(a[1])[0] for a in d.actions if a[0] == "place" and a[2] == round(bid + 0.01, 2)]
    assert ch == [51, 50, 49] and d.info["chase_sell"] == 4                     # ใกล้ราคาก่อน · ทีละ max_chase


def test_band_and_usdc_short():
    d = plan(C, {}, 5000, P0 - 0.01, P0, [], band=(P0 * 0.99, P0 * 1.01))
    placed = {parse_key(a[1])[0] for a in d.actions if a[0] == "place"}
    assert placed == {48, 49, 50, 47} and set(parse_key(k)[0] for k in d.info["waiting_band"]) == {46, 45, 44, 43}
    d = plan(C, {j: C.amount(j) for j in range(48, 100)}, 120, P0 - 0.01, P0, [])
    buys = [a for a in d.actions if a[0] == "place" and a[1].endswith("b")]
    assert len(buys) == 2 and d.info["usdc_short"] > 0                           # $120 ซื้อได้ 2 ชั้น ($50 + fee)
    assert [a for a in d.actions if a[0] == "place" and a[1].endswith("s")]     # ฝั่งขายทำงานต่อ
    # USDC ที่ติดอยู่ในออเดอร์ซื้อเดิมนับด้วย
    mine = [(key(47, "buy"), Order("A", "buy", C.buy_price(47), C.amount(47)))]
    d = plan(C, {j: C.amount(j) for j in range(48, 100)}, 60, P0 - 0.01, P0, mine)
    assert not [a for a in d.actions if a[0] == "place" and a[1].endswith("b")]


def test_allocate_existing_btc_and_cash_math():
    h = {}
    add = allocate(C, h, C.amount(48) + C.amount(49) + 0.0001, P0)
    assert add[48] == C.amount(48) and add[49] == C.amount(49) and add[50] == pytest.approx(0.0001)
    h = {j: C.amount(j) for j in range(100)}
    with pytest.raises(ValueError):
        allocate(C, h, 0.001, P0)
    sub = deallocate(C, {48: C.amount(48), 60: C.amount(60)}, C.amount(48) + 0.0001, P0)
    assert sub[48] == C.amount(48) and sub[60] == pytest.approx(0.0001)
    full = {j: C.amount(j) for j in range(48, 100)}
    assert cash_to_floor(C, full) == pytest.approx(sum(C.amount(j) * C.buy_price(j) for j in range(48)) * 1.0002, rel=1e-6)
    assert cash_to_floor(C, full) == pytest.approx(48 * 50 * 1.0002, abs=1)
    assert cash_runs_out(C, full, 48 * 50 * 1.0002 + 1, P0) is None
    assert cash_runs_out(C, full, 10 * 50 * 1.0002 + 1, P0) == C.buy_price(37)             # เงินพอ 10 ชั้น (47…38) หมดที่ชั้น 37


# ---------- จำลองทั้งระบบ ----------
class TG(Notifier):
    def __init__(self):
        super().__init__(None, None)
        self.sent = []

    async def send(self, text, key=None):
        self.sent.append(text)

    def post(self, text, key=None):
        self.sent.append(text)


def make(tmp_path, ex, cfg=C, **sf):
    store = KZMStore(str(tmp_path), cfg.fv_id)
    tg = TG()
    bot = KZMBot(cfg, ex, store, tg, live=True, safety=FVSafety(**{"settle_sec": 0.3, "rebalance_now": True, **sf}), sleep=ex.sleep, now=ex.now)
    return bot, store, tg


def own(ex, cfg=C):
    return [o for o in ex.orders.values() if parse_label(cfg, o["label"])]


def check(ex, bot, cfg=C):
    """ความจริงบน exchange: ต่อ key ไม่เกิน 1 ตัว · ไม่ติดลบ · ออเดอร์ของชั้นไม่เกินที่ชั้นนั้นขาย/ซื้อได้ (= ไม่มีวางซ้ำ)"""
    o = own(ex, cfg)
    keys = [parse_label(cfg, x["label"]) for x in o]
    assert len(keys) == len(set(keys)), ("ซ้ำ", keys)
    assert ex.base >= -1e-12 and ex.quote >= -1e-6
    h = bot.h()
    for x, kk in zip(o, keys):
        k, side = parse_key(kk)
        rem = x["amount"] - x["filled_amount"]
        if side == "sell":
            assert rem <= h.get(k, 0.0) + 1e-8 or kk in bot.pending or _lagging(bot, x), ("ขายเกินชั้น", kk, rem, h.get(k))
        else:
            assert rem <= full(cfg, k, bot.cap()) - h.get(k, 0.0) + 1e-8 or _lagging(bot, x), ("ซื้อเกินชั้น", kk, rem, h.get(k))
    for k, v in h.items():
        assert -1e-8 <= v <= full(cfg, k, bot.cap()) + 1e-8, ("บัญชีไม้ชั้นผิด", k, v)


def _lagging(bot, x):
    """fill ของออเดอร์นี้ยังไม่เข้าบัญชีไม้ (trade มาช้า) → ยอดชั้นยังเป็นยอดเก่าชั่วคราว"""
    return abs(bot.store.data["by_order"].get(x["order_id"], 0.0) - x["filled_amount"]) > 1e-10


async def settle(bot, ex, rounds=12):
    for _ in range(rounds):
        await bot.reconcile()
        check(ex, bot)
        ex.advance(2)


def assert_clean(bot, ex, cfg=C):
    e = bot.store.data["exp"]
    assert e["base"] == pytest.approx(ex.base, abs=1e-9) and e["quote"] == pytest.approx(ex.quote, abs=1e-6)
    assert sum(bot.h().values()) == pytest.approx(ex.base, abs=1e-8)
    lo, hi = ex.limits
    have = {parse_label(cfg, o["label"]): o for o in own(ex, cfg)}
    dec = plan(cfg, bot.h(), ex.quote, ex.bid, ex.ask, [], cap=bot.cap())
    for a in dec.actions:                          # ทุกออเดอร์ที่ควรมีในกรอบ ต้องมีอยู่แล้ว (เว้น USDC ไม่พอ/ล็อกหลังคำตอบหาย)
        if a[0] != "place" or a[1] in bot.pending:
            continue
        if lo and not lo <= a[2] <= hi:
            continue
        assert a[1] in have, (a, "ควรมีออเดอร์")
    for kk, o in have.items():                     # ชั้นที่ราคาทะลุไปแล้วต้องมีไม้ไล่ราคาอยู่ใกล้ราคา maker ดีที่สุด
        k, side = parse_key(kk)
        if side == "buy" and cfg.buy_price(k) >= ex.ask:
            assert abs(o["price"] - (ex.ask - cfg.tick)) <= o["price"] * cfg.chase_bps / 1e4 + 1e-6


async def scenario(tmp_path, seed, steps=400, sigma=0.004, jump=0.0, band=0.02, base=0.0, quote=5200.0, **kw):
    rng = random.Random(seed)
    ex = FakeSpot(P0, base=base, quote=quote, seed=seed, band=band, **kw)
    bot, store, tg = make(tmp_path, ex)
    await bot.startup()
    await settle(bot, ex, 4)
    for _ in range(steps):
        r = rng.gauss(0, sigma)
        if jump and rng.random() < jump:
            r = rng.choice([-1, 1]) * rng.uniform(0.01, 0.03)          # กระโดดทีเดียว 2–5 ชั้น
        ex.move_price(min(108000, max(62000, ex.bid * math.exp(r))))
        check(ex, bot)
        for _ in range(rng.randint(0, 3)):
            await bot.reconcile()
            check(ex, bot)
            ex.advance(rng.uniform(0.5, 3))
        assert store.data["status"] == "active", store.data["halt_reason"]
    b0 = ex.bid
    for i in range(40):                                           # ช่วงท้าย: แกว่ง ±0.1% ให้ไม้ไล่ราคาได้ fill
        ex.move_price(b0 * (1.001 if i % 2 == 0 else 0.999))
        await bot.reconcile()
        check(ex, bot)
        ex.advance(2)
    await settle(bot, ex, 10)
    assert store.data["status"] == "active", store.data["halt_reason"]
    assert_clean(bot, ex)
    return ex, bot, store, tg


@pytest.mark.parametrize("seed", range(4))
def test_random_walk(tmp_path, seed):
    ex, bot, store, tg = asyncio.run(scenario(tmp_path, seed))
    assert len(store.data["fills"]) > 30
    # ตอนจบ: ชั้นที่อยู่เหนือราคาเกิน 1 ชั้น ต้องถือเต็ม · ชั้นที่ขายได้ต่ำกว่าราคาเกิน 1 ชั้น ต้องว่าง (= KZM ratio)
    h = bot.h()
    for k in range(C.n):
        if C.buy_price(k) > ex.ask + C.gap:
            assert h.get(k, 0) == pytest.approx(full(C, k, bot.cap()), rel=0.01), k
        if C.sell_price(k) < ex.bid - C.gap:
            assert h.get(k, 0) * C.sell_price(k) < C.min_order_usd, k


@pytest.mark.parametrize("seed", range(4))
def test_random_walk_lag_partial_timeouts(tmp_path, seed):
    asyncio.run(scenario(tmp_path, seed, bal_lag=2, trade_lag=3, partial_prob=0.3, timeout_prob=0.15))


@pytest.mark.parametrize("seed", range(3))
def test_price_jumps(tmp_path, seed):
    asyncio.run(scenario(tmp_path, seed, jump=0.05, bal_lag=1, trade_lag=2, partial_prob=0.2))


def test_start_needs_confirmation_and_counts_existing_btc(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=0.0, quote=5200)
        bot, store, tg = make(tmp_path, ex, rebalance_now=False)
        with pytest.raises(Refuse, match="--rebalance-now"):
            await bot.startup()
        assert store.data["status"] == "new" and store.data["layers"] == {}
        # BTC ที่มีอยู่แล้วพอดีชั้นที่ควรถือ → เริ่มได้เลยไม่ต้องยืนยัน
        ex2 = FakeSpot(P0, base=FULL, quote=2500)
        bot2, store2, _ = make(tmp_path / "b", ex2, rebalance_now=False)
        await bot2.startup()
        assert sum(bot2.h().values()) == pytest.approx(FULL) and sorted(store2.data["init"]) == list(range(49, 100))
        assert all(bot2.h()[k] == A0 for k in range(49, 100)) and bot2.cap()[99] == A0
        await settle(bot2, ex2, 3)
        sides = sorted(parse_label(C, o["label"]) for o in own(ex2))
        assert sides == ["0044b", "0045b", "0046b", "0047b", "0048b", "0049s", "0050s", "0051s", "0052s", "0053s"]
        # BTC เกินกริด → ไม่เริ่ม
        ex3 = FakeSpot(P0, base=1.0, quote=0)
        bot3, _, _ = make(tmp_path / "c", ex3)
        with pytest.raises(Refuse, match="มากกว่ากริด"):
            await bot3.startup()
    asyncio.run(run())


def test_init_vs_grid_profit_split(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=0.0, quote=5200)
        bot, store, tg = make(tmp_path, ex)
        await bot.startup()
        for i in range(40):                                         # ซื้อชั้นตั้งต้นให้ครบ (ราคาแกว่งลงลึกขึ้นทีละนิด ให้ไม้ไล่ราคาที่วางตอนราคาต่ำ fill)
            ex.move_price(P0 - 0.5 - i * 0.05)
            await settle(bot, ex, 2)
            ex.move_price(P0)
            await settle(bot, ex, 2)
        assert bot.store.data["exp"]["base"] == pytest.approx(FULL + C.amount(48), rel=0.001)     # ชั้นตั้งต้น + ชั้น 48 (ซื้อที่ 84,000 ตอนแกว่ง)
        s0 = store.data["heartbeat"]["surplus"]
        rt = lambda k, b: C.amount(k) * 500 - C.amount(k) * (b + b + 500) * 0.0002     # กำไรรอบกริด 1 ชั้น หลัง fee 2 ข้าง
        ex.move_price(85100)                                        # ขึ้น → ขายชั้น 48 (ซื้อที่ 84,000 ระหว่างแกว่ง = รอบกริด) + ชั้น 49 (ชั้นตั้งต้น)
        await settle(bot, ex, 4)
        d = store.data
        assert d["init_pnl"] > 0 and 49 not in d["init"] and 50 in d["init"] and 48 not in d["init"]
        assert d["rounds"] == 1 and d["grid_pnl"] == pytest.approx(rt(48, 84000), abs=0.01)
        ex.move_price(84400)                                        # ลงซื้อชั้น 49 (84,500) อีกครั้ง
        await settle(bot, ex, 4)
        ex.move_price(85100)                                        # ขึ้นขายชั้น 49 = รอบกริดแท้
        await settle(bot, ex, 4)
        assert d["rounds"] == 2 and d["grid_pnl"] == pytest.approx(rt(48, 84000) + rt(49, 84500), abs=0.02)
        # Fix Cash ต่อชั้น: เงินสดส่วนเกิน (นอกกริด) โตเท่ากำไรที่ปิดแล้วทั้งหมด (ชั้นตั้งต้น + รอบกริด)
        assert d["heartbeat"]["surplus"] - s0 == pytest.approx(d["init_pnl"] + d["grid_pnl"], abs=0.05)
        assert any("รอบกริด" in m for m in tg.sent) and any("capital gain" in m for m in tg.sent)
    asyncio.run(run())


def test_restart_catches_fills_and_foreign_halts(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=FULL, quote=2500)
        bot, store, tg = make(tmp_path, ex)
        await bot.startup()
        await settle(bot, ex, 3)
        ex.move_price(85200)                                        # บอทหยุดอยู่ แต่ออเดอร์บน exchange fill
        bot2, store2, tg2 = make(tmp_path, ex)
        store2.data = store.data
        await bot2.startup()
        await settle(bot2, ex, 4)
        assert store2.data["exp"]["base"] == pytest.approx(ex.base, abs=1e-9) and len(store2.data["fills"]) == 1
        assert_clean(bot2, ex)
        await ex.place("buy", "BTC_USDC", 0.0001, price=70000.0, label="manual")
        await bot2.reconcile()
        assert store2.data["status"] == "halted" and "ไม่ใช่ของบอท" in store2.data["halt_reason"]
        assert not own(ex)
    asyncio.run(run())


def test_btc_deposit_halts_then_accept_allocates(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=FULL, quote=2500)
        bot, store, tg = make(tmp_path, ex, flux_halt_sec=20)
        await bot.startup()
        await settle(bot, ex, 3)
        ex.deposit(base=C.amount(48))
        for _ in range(20):
            await bot.reconcile()
            ex.advance(3)
            if store.data["status"] == "halted":
                break
        assert store.data["status"] == "halted"
        store.data.update(status="active", halt_reason="")
        bot2, _, _ = make(tmp_path, ex, accept_balance=True)
        bot2.store = store
        await bot2.startup()
        assert bot2.h()[48] == pytest.approx(C.amount(48)) and 48 in store.data["init"] and store.data["start"]["base_usd"] > FULL * P0
        await settle(bot2, ex, 3)
        assert_clean(bot2, ex)
        ex.deposit(quote=100)                                        # USDC โอนเข้า → รับยอดใหม่ ไม่หยุด ไม่นับเป็นกำไร
        await settle(bot2, ex, 12)
        assert store.data["status"] == "active" and bot2.hodl_diff(ex.bid) == pytest.approx(ex.base * ex.bid + ex.quote - store.data["start"]["base_usd"] - store.data["start"]["quote"])
        assert abs(bot2.hodl_diff(P0)) < 2
    asyncio.run(run())
