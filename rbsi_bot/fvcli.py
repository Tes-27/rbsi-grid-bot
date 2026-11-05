# -*- coding: utf-8 -*-
"""คำสั่งของบอท Fixed Value — เรียกผ่าน `python -m rbsi_bot <คำสั่ง> -c fv/<ไฟล์>.toml` (แยกจากบอทกริดด้วยหมวด [fixed_value])
  plan   : ออฟไลน์ — ราคาขาย/ซื้อ + บันไดถ้าราคาวิ่งทางเดียว + USDC หมดที่ราคาไหน
  check  : อ่านบัญชีจริง บอกว่าจะทำอะไร (ไม่ส่งคำสั่ง ไม่บันทึก ไม่ส่ง Telegram)
  run    : รันบอท (dry-run เว้นแต่ live_allowed = true และ --live)
  status : ดูไฟล์สถานะ · close : ยกเลิกออเดอร์ของบอท + ปิดบอทนี้"""
from __future__ import annotations
import asyncio, fcntl, json, os, sys, time

from .config import load_fv, load_kzm, is_kzm
from .fv import triggers, ladder, cash_runs_out, desired
from .fvbot import FVBot, FVStore, Refuse
from .store import Store, LockError
from .notify import Notifier
from .deribit import DeribitClient


def classes(C):
    """(Bot, Store) ตามชนิดไฟล์ — KZM Grid ใช้โครงเดียวกับ Fixed Value"""
    if C.get("kind") == "kzm":
        from .kzmbot import KZMBot, KZMStore
        return KZMBot, KZMStore
    return FVBot, FVStore


def kzm_plan(C, a):
    from .kzm import needs, start_plan, cash_to_floor, allocate, start_cap
    cfg = C["fv"]
    print(f"{cfg.fv_id}: {cfg.n} ชั้น × ${cfg.cost:,.2f} · ซื้อที่ floor+k·gap ขายที่ +{cfg.gap:,.0f}")
    if a.price is None:
        return
    cap = start_cap(cfg, a.price)
    h = allocate(cfg, {}, a.btc or 0.0, a.price, cap) if a.btc else {}
    sp = start_plan(cfg, h, a.price, cap)
    print(f"ราคา {a.price:,.2f}: ratio ควรถือ {sp['layers_ideal']} ชั้น → ซื้อทันที ~${sp['buy_usd']:,.2f} · BTC หลังเริ่ม {sp['btc_after']:.8f} ({sp['btc_share'] * 100:.0f}% ของทุน)")
    h2 = {**h, **cap}
    print(f"USDC ที่ต้องใช้ถ้าราคาลงถึง floor: {cash_to_floor(cfg, h2, cap, a.price):,.2f}")
    near = sorted(needs(cfg, h2, cap).items(), key=lambda x: abs(cfg.buy_price(x[0]) - a.price))[: a.levels * 2]
    for k, w in sorted(near, reverse=True):
        for side, amt in w.items():
            px = cfg.buy_price(k) if side == "buy" else cfg.sell_price(k)
            print(f"  ชั้น {k:>4} {'ขาย' if side == 'sell' else 'ซื้อ'} {px:>12,.2f} × {amt:.8f} = ${amt * px:,.2f}")


def cmd_plan(C, a):
    if C.get("kind") == "kzm":
        return kzm_plan(C, a)
    cfg = C["fv"]
    print(f"Fixed Value {cfg.instrument}: เป้า ${cfg.value:,.2f} ± ${cfg.delta:,.2f} ({cfg.delta / cfg.value * 100:.2f}%) · id {cfg.fv_id}")
    if a.price is None:
        return
    qty = a.btc if a.btc is not None else cfg.value / a.price
    cash = a.usdc or 0.0
    ps, pb = triggers(cfg, qty)
    print(f"ถือ {qty:.8f} {cfg.base} (${qty * a.price:,.2f} ที่ {a.price:,.2f}) + {cash:,.2f} {cfg.quote}")
    for side in ("sell", "buy"):
        d = desired(cfg, side, qty, a.price - cfg.tick, a.price)
        if d:
            print(f"  {'ขาย' if side == 'sell' else 'ซื้อ'}ที่ {d[0]:>12,.2f} × {d[1]:.8f} = ${d[0] * d[1]:,.2f}")
    print("บันได (ถ้าราคาวิ่งทางเดียวทีละขั้น):")
    for side, price, amt, q, c, note in ladder(cfg, qty, cash, a.levels):
        print(f"  {'ขาย' if side == 'sell' else 'ซื้อ'} {price:>12,.2f} × {amt:.8f} → ถือ {q:.8f} · {cfg.quote} {c:,.2f} {note}")
    out = cash_runs_out(cfg, qty, cash)
    print(f"{cfg.quote} หมดที่ราคา ≈ {out:,.2f} ({(out / a.price - 1) * 100:+.1f}%)" if out else f"{cfg.quote} ไม่หมดก่อนราคาเหลือ 1%")


async def build(C, live, a, lock=True):
    cfg = C["fv"]
    if not (C["client_id"] and C["client_secret"]):
        sys.exit(f"ไม่พบ key ของบัญชีนี้ใน {C['env_path']}")
    Bot, St = classes(C)
    store = St(C["state_dir"], cfg.fv_id)
    if lock:
        try:
            store.lock(cfg.fv_id)
            store.lock(Store.key_lock_name(C["client_id"]))
        except LockError as e:
            sys.exit(str(e))
    sf = C["safety"]
    sf.rebalance_now = bool(getattr(a, "rebalance_now", False))
    sf.accept_balance = bool(getattr(a, "accept_balance", False))
    pre = f"[{C['name'] or cfg.fv_id}{' TESTNET' if C['testnet'] else ''}] "
    return Bot(cfg, DeribitClient(C["testnet"], C["client_id"], C["client_secret"]), store,
                 Notifier(C["tg_token"], C["tg_chat"], prefix=pre), live=live, safety=sf), store


async def cmd_check(C, a):
    bot, store = await build(C, False, a)
    bot.n = Notifier(None, None)
    await bot.c.connect()
    try:
        if store.data["status"] == "halted":
            print(f"🛑 สถานะ halt: {store.data['halt_reason']} (รันใหม่ต้อง --clear-halt) — ตรวจต่อแบบสมมติว่าเคลียร์แล้ว")
            store.data["status"] = "active"
        try:
            r, notes = await bot.startup()
        except Refuse as e:
            print("🛑 เริ่มไม่ได้:", e)
            return
        for n in notes:
            print(n)
        await bot.reconcile()
        d = bot.last
        if d is None:
            print("ยังตัดสินใจไม่ได้ (ข้อมูลไม่นิ่ง) — ลองใหม่")
            return
        i = d.info
        cfg = C["fv"]
        if C.get("kind") == "kzm":
            print(f"ราคา {i['mid']:,.2f} · ถือ {i['layers_held']}/{cfg.n} ชั้น {cfg.base} ${i['value']:,.2f} · {cfg.quote} {store.data['exp']['quote']:,.2f}")
            print(f"ซื้อถัดไป {i['next_buy'] or 0:,.2f} · ขายถัดไป {i['next_sell'] or 0:,.2f} · ชั้นที่ต้องไล่ราคา ซื้อ {i['chase_buy']} ขาย {i['chase_sell']}")
            for x in d.actions:
                print("  จะทำ:", bot.describe(x))
            if not d.actions:
                print("  ไม่ต้องทำอะไร — ออเดอร์ครบแล้ว")
            for kk, px in sorted(i["waiting_band"].items()):
                print(f"  รอราคาเข้ากรอบ Deribit: {kk} @ {px:,.2f}")
            if i.get("usdc_short"):
                print(f"  ⚠️ {cfg.quote} ไม่พอซื้อ (ต้องการ ~{i['usdc_short']:,.2f})")
            return
        print(f"ราคา {i['mid']:,.2f} · {cfg.base} ${i['value']:,.2f} ({i['dev']:+,.2f}) · {cfg.quote} {store.data['exp']['quote']:,.2f}")
        print(f"ขายเมื่อราคา ≥ {i['sell_at'] or 0:,.2f} · ซื้อเมื่อราคา ≤ {i['buy_at'] or 0:,.2f}")
        for x in d.actions:
            print("  จะทำ:", bot.describe(x))
        if not d.actions:
            print("  ไม่ต้องทำอะไร — ออเดอร์ครบแล้ว")
        if i.get("usdc_short"):
            print(f"  ⚠️ {cfg.quote} ไม่พอซื้อ (ต้องการ ~{i['usdc_short']:,.2f})")
    finally:
        await bot.c.close()
        store.unlock()


async def cmd_run(C, a):
    live = bool(a.live)
    if live and not C["live_allowed"]:
        sys.exit("config ยังไม่อนุญาต live (ตั้ง live_allowed = true)")
    bot, store = await build(C, live, a)
    st = store.data["status"]
    if st == "closed":
        sys.exit("บอทนี้ปิดไปแล้ว")
    if st == "halted":
        if not a.clear_halt:
            sys.exit(f"บอทถูก halt: {store.data['halt_reason']} — ตรวจแล้วรันด้วย --clear-halt")
        store.data.update(status="active", halt_reason="")
    print("จบ:", await bot.run())


async def cmd_close(C, a):
    bot, store = await build(C, bool(a.live), a)
    await bot.c.connect()
    try:
        await bot.close("สั่งปิดจาก CLI")
    finally:
        await bot.c.close()


def cmd_status(C):
    cfg = C["fv"]
    st = classes(C)[1](C["state_dir"], cfg.fv_id).data
    lp = os.path.join(os.path.expanduser(C["state_dir"]), f"{cfg.fv_id}.lock")
    running = False
    if os.path.exists(lp):
        fd = os.open(lp, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            running = True
        finally:
            os.close(fd)
    print(f"process: {'🟢 กำลังรัน' if running else '⚪ ไม่ได้รัน'} · สถานะ: {st.get('status')}" + (f" ({st['halt_reason']})" if st.get("halt_reason") else ""))
    hb = st.get("heartbeat") or {}
    if hb and C.get("kind") == "kzm":
        print(f"ตรวจล่าสุด {time.time() - hb['ts']:,.0f} วิที่แล้ว · ราคา {hb['mid']:,.2f} · ถือ {hb['layers_held']}/{cfg.n} ชั้น "
              f"· {cfg.base} ${hb['value']:,.2f} · {cfg.quote} {hb['quote']:,.2f} · ออเดอร์ {hb['own']} · กริดแท้ {st.get('grid_pnl', 0):+,.2f}$ ({st.get('rounds', 0)} รอบ)")
    elif hb:
        print(f"ตรวจล่าสุด {time.time() - hb['ts']:,.0f} วิที่แล้ว · ราคา {hb['mid']:,.2f} · {cfg.base} ${hb['value']:,.2f} ({hb['dev']:+,.2f}) "
              f"· {cfg.quote} {hb['quote']:,.2f} · ออเดอร์ของบอท {hb['own']}/2" + (f" · เทียบถือเฉย ๆ {hb['hodl']:+,.2f}$" if hb.get("hodl") is not None else ""))
    for f in (st.get("fills") or [])[-5:]:
        print(f"  fill {time.strftime('%m-%d %H:%M', time.localtime(f['ts'] / 1000))} {f['side'].upper():<4} {f['amount']:.8f} @ {f['price']:,.2f}")
    print(f"log: {os.path.join(os.path.expanduser(C['state_dir']), cfg.fv_id + '.log')}")


def main(a, setup_log):
    C = load_kzm(a.config) if is_kzm(a.config) else load_fv(a.config)
    if a.cmd == "plan":
        return cmd_plan(C, a)
    if a.cmd == "status":
        return cmd_status(C)
    if a.cmd == "reopen":
        cfg = C["fv"]
        store = classes(C)[1](C["state_dir"], cfg.fv_id)
        try:
            store.lock(cfg.fv_id)
        except LockError as e:
            sys.exit(f"{e} — หยุดบอทก่อน")
        try:
            dst = store.archive_closed()
        except ValueError as e:
            sys.exit(str(e))
        print(f"เปิดบอท {cfg.fv_id} ใหม่แล้ว · สถานะเก่าเก็บไว้ที่ {dst}")
        print("ขั้นต่อไป: ตรวจ / เริ่ม — บอทจะเริ่มนับใหม่จากยอด BTC/USDC ในบัญชีตอนนั้น (เทียบถือเฉย ๆ นับใหม่)")
        return
    setup_log(C["state_dir"], C["fv"].fv_id)
    asyncio.run({"check": cmd_check, "run": cmd_run, "close": cmd_close}[a.cmd](C, a))
