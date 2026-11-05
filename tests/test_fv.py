# -*- coding: utf-8 -*-
"""บอท Fixed Asset Value: ตรรกะล้วน + จำลองทั้งระบบบน FakeSpot (ข้อมูลช้า / fill บางส่วน / คำตอบหาย / รีสตาร์ท / โอนเงิน)"""
import asyncio, math, random
import pytest
from rbsi_bot.fv import FVConfig, triggers, desired, consistent, plan, ladder, cash_runs_out, order_amount
from rbsi_bot.fvbot import FVBot, FVStore, FVSafety, Refuse
from rbsi_bot.grid import Order
from rbsi_bot.notify import Notifier
from tests.fake_spot import FakeSpot

C = FVConfig(3500, "usd", 100)
P0 = 84000.0
Q0 = round(3500 / P0, 8)


# ---------- ตรรกะล้วน ----------
def test_config():
    assert C.delta == 100 and C.fv_id == "BTCUSDC-V3500-D100" and C.label("sell") == "fv_BTCUSDC-V3500-D100_s"
    p = FVConfig(3500, "pct", 2.5)
    assert p.delta == 87.5 and p.fv_id == "BTCUSDC-V3500-P2p5"
    for bad in (dict(value=0), dict(mode="x"), dict(step=2000), dict(mode="pct", step=60), dict(instrument="BTC-PERPETUAL")):
        with pytest.raises(ValueError):
            FVConfig(**{"value": 3500, **bad})


def test_normal_orders_trade_exactly_delta():
    ps, pb = triggers(C, Q0)
    assert ps == pytest.approx(3600 / Q0) and pb == pytest.approx(3400 / Q0)
    sp, sa = desired(C, "sell", Q0, P0 - 0.01, P0)
    bp, ba = desired(C, "buy", Q0, P0 - 0.01, P0)
    assert sp == round(sp, 2) and sa == round(sa, 8) and bp == round(bp, 2) and ba == round(ba, 8)
    assert sa * sp == pytest.approx(100, abs=0.01) and ba * bp == pytest.approx(100, abs=0.01)
    assert (Q0 - sa) * sp == pytest.approx(3500, abs=0.01) and (Q0 + ba) * bp == pytest.approx(3500, abs=0.01)


def test_crossed_trigger_rebalances_fully_at_best_maker_price():
    # ราคาอยู่เหนือ trigger ขายแล้ว (มูลค่า $3,650) → ขายที่ bid+tick ให้ครบกลับเข้า $3,500 ในไม้เดียว
    bid = round(3650 / Q0, 2)
    p, amt = desired(C, "sell", Q0, bid, bid + 0.01)
    assert p == round(bid + 0.01, 2) and (Q0 - amt) * p == pytest.approx(3500, abs=0.01) and amt * p > 140
    ask = round(3250 / Q0, 2)
    p, amt = desired(C, "buy", Q0, ask - 0.01, ask)
    assert p == round(ask - 0.01, 2) and (Q0 + amt) * p == pytest.approx(3500, abs=0.01)
    assert desired(C, "sell", 0.0, P0, P0 + 0.01) is None                          # ไม่มี BTC ไม่มีอะไรให้ขาย
    capped = FVConfig(3500, "usd", 100, max_order_usd=120)
    assert desired(capped, "sell", Q0, bid, bid + 0.01)[1] * (bid + 0.01) <= 120.0001


def test_partial_fill_order_stays_consistent():
    sp, sa = desired(C, "sell", Q0, P0 - 0.01, P0)
    assert consistent(C, "sell", Q0, sp, sa)
    half = round(sa / 2, 8)
    assert consistent(C, "sell", Q0 - half, sp, round(sa - half, 8))                # fill ครึ่งหนึ่ง → ที่เหลือยังถูก
    assert not consistent(C, "sell", Q0 + 0.001, sp, sa)                           # ยอดเปลี่ยนจากไม้อื่น → ต้องวางใหม่
    assert not consistent(C, "buy", Q0, sp, sa)


def test_plan_one_order_per_side_and_no_replace_in_same_cycle():
    sp, sa = desired(C, "sell", Q0, P0 - 0.01, P0)
    good = Order("A", "sell", sp, sa)
    dup = Order("B", "sell", sp, sa)
    stale = Order("C", "buy", 70000.0, 0.001)
    d = plan(C, Q0, 3600, P0 - 0.01, P0, [good, dup, stale])
    assert ("cancel", "B", "sell", "ซ้ำ") in d.actions and any(a[:2] == ("cancel", "C") for a in d.actions)
    assert not [a for a in d.actions if a[0] == "place"]                           # ยกเลิกก่อน วางใหม่รอบหน้า
    d = plan(C, Q0, 3600, P0 - 0.01, P0, [good], pending={"buy"})
    assert d.actions == []
    d = plan(C, Q0, 50.0, P0 - 0.01, P0, [])
    assert [a[1] for a in d.actions] == ["sell"] and d.info["usdc_short"] > 50     # USDC ไม่พอ → หยุดแค่ฝั่งซื้อ


def test_chase_reprices_only_when_trigger_still_crossed():
    # ราคากระโดดลง → วางซื้อไล่ที่ ask−tick · ตลาดเด้งขึ้น 0.1% แต่ยังใต้ trigger → ยกเลิกเพื่อวางใหม่ที่ราคาดีสุด
    ask = round(3250 / Q0, 2)
    p, amt = desired(C, "buy", Q0, ask - 0.01, ask)
    o = Order("A", "buy", p, amt)
    up = round(ask * 1.001, 2)
    d = plan(C, Q0, 3600, up - 0.01, up, [o])
    assert ("cancel", "A", "buy", "ไล่ราคา") in d.actions and not any(a[:2] == ("place", "buy") for a in d.actions)
    d = plan(C, Q0, 3600, ask - 0.01 + 1, ask + 1, [o])                  # ขยับนิดเดียว (< 0.05%) → คงไว้
    assert d.actions == [] or all(a[1] != "A" for a in d.actions)
    # ไม้ปกติที่รอ trigger (ตลาดยังไม่ถึง) ไม่ถูกไล่ แม้ห่างราคาตลาดมาก
    bp, ba = desired(C, "buy", Q0, P0 - 0.01, P0)
    d = plan(C, Q0, 3600, P0 - 0.01, P0, [Order("B", "buy", bp, ba)])
    assert all(a[1] != "B" for a in d.actions if a[0] == "cancel")


def test_ladder_and_cash_exhaustion():
    rows = ladder(C, Q0, 3600, 4)
    assert all(r[2] * r[1] == pytest.approx(100, abs=0.02) for r in rows)
    ups = [r[1] for r in rows if r[0] == "sell"]
    assert ups[1] / ups[0] == pytest.approx(3600 / 3500, rel=1e-5)                 # บันไดเรขาคณิต อัตราส่วน (V+Δ)/V
    out = cash_runs_out(C, Q0, 3600)
    assert out / P0 == pytest.approx(math.exp(-3600 / 3500), abs=0.02)            # ~ P0·e^(−cash/V) ≈ −64%


# ---------- จำลองทั้งระบบ ----------
class TG(Notifier):
    def __init__(self):
        super().__init__(None, None)
        self.sent = []
    async def send(self, text, key=None):
        self.sent.append(text)


def make(tmp_path, ex, cfg=C, **sf):
    store = FVStore(str(tmp_path), cfg.fv_id)
    tg = TG()
    bot = FVBot(cfg, ex, store, tg, live=True, safety=FVSafety(**{"settle_sec": 0.3, **sf}), sleep=ex.sleep, now=ex.now)
    return bot, store, tg


def own(ex, cfg=C):
    return [o for o in ex.orders.values() if o["label"].startswith(f"fv_{cfg.fv_id}_")]


def check(ex, cfg=C):
    """ความจริงบน exchange: ฝั่งละไม่เกิน 1 ตัว · ไม่ติดลบ · ไม่มีขายเกินยอดที่ทำให้มูลค่าต่ำกว่าเป้า (= ไม่มีวางซ้ำ)"""
    o = own(ex, cfg)
    assert len([x for x in o if x["direction"] == "sell"]) <= 1 and len([x for x in o if x["direction"] == "buy"]) <= 1, o
    assert ex.base >= -1e-12 and ex.quote >= -1e-6
    for x in o:
        rem = x["amount"] - x["filled_amount"]
        if x["direction"] == "sell":
            assert (ex.base - rem) * x["price"] >= cfg.value - 0.05, ("ขายเกิน", x, ex.base)
        else:
            assert (ex.base + rem) * x["price"] <= cfg.value + 0.05, ("ซื้อเกิน", x, ex.base)


async def settle(bot, ex, rounds=12):
    for _ in range(rounds):
        await bot.reconcile()
        check(ex)
        ex.advance(2)


def assert_clean(bot, ex, cfg=C):
    mid = (ex.bid + ex.ask) / 2
    e = bot.store.data["exp"]
    assert e["base"] == pytest.approx(ex.base, abs=1e-9) and e["quote"] == pytest.approx(ex.quote, abs=1e-6)
    dev = ex.base * mid - cfg.value
    if abs(dev) >= cfg.delta:
        # นอกกรอบได้เฉพาะตอนมีไม้ไล่ราคารออยู่ที่ราคา maker ดีที่สุด (fill แล้วกลับเข้าเป้า) — ตลาดจริงขยับมา fill เอง
        side = "sell" if dev > 0 else "buy"
        best = ex.bid + cfg.tick if side == "sell" else ex.ask - cfg.tick
        o = [x for x in own(ex, cfg) if x["direction"] == side]
        assert o and abs(o[0]["price"] - best) <= best * cfg.chase_bps / 1e4 + 1e-9, (dev, o, "มูลค่าหลุดกรอบแต่ไม่มีไม้ไล่ราคา")
        rem = o[0]["amount"] - o[0]["filled_amount"]
        after = (ex.base - rem if side == "sell" else ex.base + rem) * o[0]["price"]
        assert after == pytest.approx(cfg.value, abs=1) or (cfg.max_order_usd and rem * o[0]["price"] >= cfg.max_order_usd - 1)
    # ฝั่งที่ราคาตามสูตรอยู่ในกรอบราคาของ exchange ต้องมีออเดอร์ · นอกกรอบต้องยังไม่วาง (รอราคาเข้าใกล้)
    lo, hi = ex.limits
    q = bot.store.data["exp"]["base"]
    for side in ("sell", "buy"):
        want = desired(cfg, side, q, ex.bid, ex.ask)
        have = [o for o in own(ex, cfg) if o["direction"] == side]
        if want and (not lo or lo <= want[0] <= hi):
            if side == "buy" and want[0] * want[1] * 1.0002 > ex.quote:
                continue                                               # USDC ไม่พอ
            if side in bot.pending:
                continue                                               # ล็อกหลังคำตอบหาย (≤ pending_ttl) — ออกแบบให้รอ ไม่ส่งซ้ำ
            assert have, (side, want, "ควรมีออเดอร์")
        elif want and not (dev >= cfg.delta and side == "sell" or dev <= -cfg.delta and side == "buy"):
            assert not have or lo <= have[0]["price"] <= hi, (side, have, "ไม่ควรมีออเดอร์นอกกรอบ")


async def scenario(tmp_path, seed, steps=400, sigma=0.01, jump=0.0, band=0.02, **kw):
    rng = random.Random(seed)
    ex = FakeSpot(P0, base=Q0, quote=3600, seed=seed, band=band, **kw)
    bot, store, tg = make(tmp_path, ex)
    await bot.startup()
    await settle(bot, ex, 4)
    for _ in range(steps):
        r = rng.gauss(0, sigma)
        if jump and rng.random() < jump:
            r = rng.choice([-1, 1]) * rng.uniform(0.05, 0.12)          # กระโดดทีเดียว 2–4 ขั้น
        ex.move_price(min(130000, max(40000, ex.bid * math.exp(r))))
        check(ex)
        for _ in range(rng.randint(0, 3)):
            await bot.reconcile()
            check(ex)
            ex.advance(rng.uniform(0.5, 3))
        assert store.data["status"] == "active", store.data["halt_reason"]
    base = ex.bid
    for i in range(30):                                           # ช่วงท้าย: ราคาแกว่ง ±0.1% รอบจุดเดิม ให้ post-only ที่ bid/ask ได้ fill (ตลาดจริงขยับเสมอ)
        ex.move_price(base * (1.001 if i % 2 == 0 else 0.999))
        await bot.reconcile()
        check(ex)
        ex.advance(2)
    await settle(bot, ex, 10)
    assert store.data["status"] == "active", store.data["halt_reason"]
    assert_clean(bot, ex)
    return ex, bot, store, tg


@pytest.mark.parametrize("seed", range(4))
def test_random_walk(tmp_path, seed):
    ex, bot, store, tg = asyncio.run(scenario(tmp_path, seed))
    assert len(store.data["fills"]) > 20


@pytest.mark.parametrize("seed", range(4))
def test_random_walk_lag_partial_timeouts(tmp_path, seed):
    asyncio.run(scenario(tmp_path, seed, bal_lag=2, trade_lag=3, partial_prob=0.3, timeout_prob=0.15))


@pytest.mark.parametrize("seed", range(4))
def test_price_jumps_multiple_steps(tmp_path, seed):
    ex, bot, store, tg = asyncio.run(scenario(tmp_path, seed, jump=0.05, bal_lag=1, trade_lag=2, partial_prob=0.2))
    big = [f for f in store.data["fills"] if f["amount"] * f["price"] > 150]
    assert big, "ต้องมีไม้ที่ปรับหลายขั้นในไม้เดียว"


def test_fill_message_and_hodl(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600)
        bot, store, tg = make(tmp_path, ex)
        await bot.startup()
        await settle(bot, ex, 3)
        ex.move_price(86500)                                         # ขาย $100 ที่ 86,400
        await settle(bot, ex, 3)
        await bot.n.flush()
        msg = [m for m in tg.sent if "ขาย" in m][0]
        assert "@ 86,400.00" in msg and "$100.00" in msg and "เทียบถือเฉย ๆ" in msg and "(เป้า $3,500)" in msg
        ex.move_price(84000)                                         # ลงกลับมา → ซื้อคืนที่ ~84,000 (รอบไป-กลับ = ได้เปรียบถือเฉย ๆ)
        await settle(bot, ex, 3)
        assert bot.hodl_diff(84000) > 2.0
    asyncio.run(run())


def test_restart_catches_up_fills_while_down(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600)
        bot, store, _ = make(tmp_path, ex)
        await bot.startup()
        await settle(bot, ex, 3)
        store.save()
        ex.move_price(86500)                                         # บอทหยุดอยู่ ไม้ขายที่รอไว้ fill เอง
        bot2, store2, tg2 = make(tmp_path, ex)
        r, notes = await bot2.startup()
        assert any("ระหว่างบอทหยุด" in n for n in notes)
        await settle(bot2, ex, 4)
        assert_clean(bot2, ex)
    asyncio.run(run())


def test_start_refuses_when_far_from_target_until_confirmed(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0 * 1.06, quote=3600)                 # มูลค่า ~$3,710
        bot, store, _ = make(tmp_path, ex)
        with pytest.raises(Refuse, match="--rebalance-now"):
            await bot.startup()
        assert store.data["status"] == "new"
        bot, store, _ = make(tmp_path, ex, rebalance_now=True)
        await bot.startup()
        await settle(bot, ex, 2)
        s = [o for o in own(ex) if o["direction"] == "sell"][0]
        assert s["price"] == ex.ask and (ex.base - s["amount"]) * s["price"] == pytest.approx(3500, abs=0.05)
    asyncio.run(run())


def test_btc_deposit_halts_and_accept_balance_resumes(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600)
        bot, store, tg = make(tmp_path, ex, flux_halt_sec=20)
        await bot.startup()
        await settle(bot, ex, 3)
        ex.deposit(base=0.001)                                       # โอน BTC เข้า sub
        for _ in range(20):
            await bot.reconcile()
            ex.advance(2)
        assert store.data["status"] == "halted" and "--accept-balance" in store.data["halt_reason"]
        assert own(ex) == []                                         # halt = ยกเลิกออเดอร์ของบอท
        store.data["status"] = "active"                              # (--clear-halt)
        bot2, store2, _ = make(tmp_path, ex)
        with pytest.raises(Refuse, match="--accept-balance"):
            await bot2.startup()
        bot3, store3, _ = make(tmp_path, ex, accept_balance=True, rebalance_now=True)
        store3.data["status"] = "active"
        await bot3.startup()
        assert bot3.hodl_diff(ex.bid) == pytest.approx(0, abs=0.5)    # เงินโอนไม่นับเป็นกำไร
        await settle(bot3, ex, 6)
        assert_clean(bot3, ex)
    asyncio.run(run())


def test_usdc_deposit_accepted_without_halt(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600)
        bot, store, tg = make(tmp_path, ex, cash_accept_sec=5)
        await bot.startup()
        await settle(bot, ex, 3)
        ex.deposit(quote=500)
        await settle(bot, ex, 6)
        await bot.n.flush()
        assert store.data["status"] == "active" and store.data["exp"]["quote"] == pytest.approx(4100, abs=0.01)
        assert any("เปลี่ยนนอกบอท +500.00" in m for m in tg.sent)
        assert bot.hodl_diff(ex.bid) == pytest.approx(0, abs=0.01)
    asyncio.run(run())


def test_manual_order_or_trade_halts(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600)
        bot, store, _ = make(tmp_path, ex)
        await bot.startup()
        await settle(bot, ex, 2)
        await ex.place("buy", "BTC_USDC", 0.0001, price=80000.0, label="")       # ผู้ใช้วางเองใน sub ของบอท
        await bot.reconcile()
        assert store.data["status"] == "halted" and "ไม่ใช่ของบอท" in store.data["halt_reason"]
    asyncio.run(run())


def test_manual_order_at_start_refuses(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600)
        await ex.place("sell", "BTC_USDC", 0.0001, price=90000.0, label="")
        bot, store, _ = make(tmp_path, ex)
        with pytest.raises(Refuse, match="ไม่ใช่ของบอท"):
            await bot.startup()
    asyncio.run(run())


def test_usdc_short_stops_buys_only(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=30)
        bot, store, tg = make(tmp_path, ex)
        await bot.startup()
        await settle(bot, ex, 3)
        assert sorted(o["direction"] for o in own(ex)) == ["sell"]
        await bot.n.flush()
        assert any("ไม่พอซื้อ" in m for m in tg.sent)
    asyncio.run(run())


# ---------- CLI ต่อสายครบ (config → build → check / run) ----------
def _fv_config(tmp_path, live=True):
    env = tmp_path / ".env"
    env.write_text("DERIBIT_CLIENT_ID_FVT=fvtestid1\nDERIBIT_CLIENT_SECRET_FVT=fvtestsecret123\n", encoding="utf-8")
    env.chmod(0o600)
    d = tmp_path / "fv"
    d.mkdir()
    p = d / "t.toml"
    p.write_text(f'''name = "FVT"
env_file = "../.env"
state_dir = "{tmp_path / 'st'}"
live_allowed = {"true" if live else "false"}
[deribit]
client_id_env = "DERIBIT_CLIENT_ID_FVT"
client_secret_env = "DERIBIT_CLIENT_SECRET_FVT"
[fixed_value]
value = 3500
mode = "pct"
step = 3
[safety]
settle_sec = 0.3
''', encoding="utf-8")
    return str(p)


def test_cli_check_and_run_wiring(tmp_path, monkeypatch, capsys):
    import argparse
    from rbsi_bot import fvcli
    from rbsi_bot.config import load_fv, is_fv
    path = _fv_config(tmp_path)
    assert is_fv(path)
    C = load_fv(path)
    assert C["fv"].delta == 105 and C["client_id"] == "fvtestid1" and C["fv"].fv_id == "BTCUSDC-V3500-P3"
    ex = FakeSpot(P0, base=Q0, quote=3600)
    monkeypatch.setattr(fvcli, "DeribitClient", lambda *a, **k: ex)
    a = argparse.Namespace(rebalance_now=False, accept_balance=False, live=True, clear_halt=False)
    asyncio.run(fvcli.cmd_check(C, a))
    out = capsys.readouterr().out
    assert "จะทำ: SELL" in out and "จะทำ: BUY" in out and ex.orders == {}              # check ไม่ส่งคำสั่ง
    # run: ให้บอทวางออเดอร์ แล้วปิดเอง (สถานะ closed) หลังวางครบ
    async def run_then_close():
        C2 = load_fv(path)
        bot, store = await fvcli.build(C2, True, a)
        async def closer():
            while len(own(ex, C2["fv"])) < 2:
                await asyncio.sleep(0.01)
            store.data["status"] = "closed"
            bot.wake.set()
        bot.sf.reconcile_sec = 0.01
        return await asyncio.wait_for(asyncio.gather(bot.run(), closer()), 10)
    res = asyncio.run(run_then_close())
    assert res[0] == "closed" and sorted(o["direction"] for o in own(ex, C["fv"])) == ["buy", "sell"]


def test_plan_waits_for_price_band():
    """Deribit spot รับออเดอร์เฉพาะใน min_price–max_price (~±2% จาก index) — นอกกรอบถูกยกเลิกทิ้ง (เจอจริง: ขาย 106,343 ถูกยกเลิก)"""
    band = (P0 * 0.98, P0 * 1.02)
    d = plan(C, Q0, 3600, P0 - 0.01, P0, [], band=band)                   # Δ 2.86% > กรอบ 2% → ยังไม่วางทั้งสองฝั่ง
    assert d.actions == [] and set(d.info["waiting_band"]) == {"sell", "buy"}
    near = P0 * 1.01                                                       # ราคาขยับขึ้น 1% → ราคาขาย 86,400 อยู่ในกรอบแล้ว
    d = plan(C, Q0, 3600, near - 0.01, near, [], band=(near * 0.98, near * 1.02))
    assert [a[1] for a in d.actions if a[0] == "place"] == ["sell"] and "buy" in d.info["waiting_band"]


def test_exchange_band_cancels_do_not_break_bot(tmp_path):
    async def run():
        ex = FakeSpot(P0, base=Q0, quote=3600, band=0.02)
        bot, store, _ = make(tmp_path, ex)
        await bot.startup()
        await settle(bot, ex, 3)
        assert own(ex) == [] and ex.placed == 0                           # ยังไม่วางอะไรนอกกรอบ
        for p in (84800, 85600, 86500):
            ex.move_price(p)
            await settle(bot, ex, 3)
        assert len(store.data["fills"]) == 1 and store.data["fills"][0]["side"] == "sell" and ex.band_cancels == 0
    asyncio.run(run())


def test_reopen_closed_fv_bot(tmp_path):
    store = FVStore(str(tmp_path), C.fv_id)
    store.data.update(status="closed", start={"base": 1.0, "quote": 2.0})
    store.save()
    dst = store.archive_closed()
    assert ".closed-" in dst and store.data["status"] == "new" and store.data["kind"] == "fv" and store.data["start"] == {}
    assert FVStore(str(tmp_path), C.fv_id).data["status"] == "new"
