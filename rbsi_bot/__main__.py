# -*- coding: utf-8 -*-
"""CLI
  plan   : แสดงชั้น/ออเดอร์ที่ควรมี ณ ราคาหนึ่ง (ออฟไลน์ ไม่ต่อเน็ต)
  check  : ต่อ Deribit อ่านสถานะจริง แล้วบอกว่าจะทำอะไร (ไม่ส่งคำสั่ง)
  run    : รันบอท (ค่าเริ่ม dry-run · live ต้องมีทั้ง live_allowed = true ใน config และ --live)
  close  : ยกเลิกออเดอร์ของบอทใน zone นี้ + ปิด zone ถาวร
  reopen : เปิด zone ที่ปิดแล้วใหม่ด้วยกรอบ/gap/ขนาดเดิม (เก็บสถานะเก่าเป็นไฟล์สำรอง แล้วรับช่วงใหม่ตอน run)
  status : ดูไฟล์สถานะ zone
  tg     : ตั้งค่า Telegram — ยังไม่มี TG_CHAT_ID = แสดง chat id จากข้อความที่ส่งหาบอท · มีแล้ว = ส่งข้อความทดสอบ
  ui     : หน้าเว็บควบคุมในเครื่อง (ตั้ง zone + ดึงบัญชีจาก .env + เริ่ม/หยุดบอท) — ดู rbsi_bot/ui.py"""
from __future__ import annotations
import argparse, asyncio, json, logging, math, os, sys

from .config import load
from .grid import expected, m_range
from .store import Store, LockError
from .notify import Notifier
from .deribit import DeribitClient
from .bot import GridBot, Halt


def setup_log(state_dir, zone_id):
    d = os.path.expanduser(state_dir)
    os.makedirs(d, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(os.path.join(d, f"{zone_id}.log"), encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def cmd_plan(C, a):
    z = C["zone"]
    print(f"zone {z.zone_id} ({z.word.upper()}): {z.lower:,.0f}–{z.upper:,.0f} gap {z.gap:,.0f} × {z.size:,} USD = {z.n} ชั้น · เพดาน {z.word} {z.cap:,} USD")
    if a.price is None:
        return
    lo, hi = m_range(z, a.price)
    x = ((z.upper - a.price) if z.is_long else (a.price - z.lower)) / z.gap
    m = a.m if a.m is not None else max(lo, min(hi, math.ceil(x - 1e-9)))
    print(f"ราคา {a.price:,.1f} → {z.word} ได้ {lo}–{hi} ชั้น (ขึ้นกับว่าราคาเพิ่งขึ้นหรือลงมา) · แสดง m = {m} (position {(m if z.is_long else -m) * z.size:+,}) ใช้ --m เปลี่ยนได้")
    for k, (side, px) in sorted(expected(z, m).items(), key=lambda x: -x[1][1]):
        print(f"  ชั้น {k:>3}  {side.upper():<4} {px:>10,.1f} × {z.size:,}{'  reduce-only' if side == z.close_side else ''}   label {z.label(k)}")


async def build(C, live, a):
    z = C["zone"]
    if not (C["client_id"] and C["client_secret"]):
        sys.exit(f"ไม่พบ DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET — ใส่ใน {C.get('env_path')}")
    store = Store(C["state_dir"], z.zone_id)
    try:
        store.lock(z.zone_id)
        store.lock(Store.key_lock_name(C["client_id"]))
    except LockError as e:
        sys.exit(str(e))
    C["safety"].adopt_manual = getattr(a, "adopt_manual", False)
    C["safety"].force = getattr(a, "force", False)
    pre = f"[{C['name'] or z.zone_id}{' TESTNET' if C['testnet'] else ''}] "
    bot = GridBot(z, DeribitClient(C["testnet"], C["client_id"], C["client_secret"]), store,
                  Notifier(C["tg_token"], C["tg_chat"], prefix=pre), live=live, safety=C["safety"])
    return bot, store


async def cmd_check(C, a):
    bot, store = await build(C, False, a)
    bot.n = Notifier(None, None)                   # check แค่ตรวจ ไม่ส่งข้อความเข้า Telegram
    await bot.c.connect()
    try:
        if store.data["status"] == "new":
            r, m0 = await bot.adopt()
            print(f"รับช่วงได้: position {r['position']:+,.0f} = {m0} ชั้น (ยังไม่บันทึก — check ไม่เปลี่ยนอะไร)")
            r = await bot.read()
        else:
            r = await bot.read()
            new, got = await bot.catch_up(r)
            for t, k in new:
                print(f"fill ระหว่างบอทหยุด: {t['direction'].upper()} {float(t['price']):,.1f} × {float(t['amount']):,.0f} ชั้น {k}")
            for k, side, px in got:
                print(f"จะรับออเดอร์มือเป็นของบอท: ชั้น {k} {side.upper()} {px:,.1f}")
        print(await bot.preflight(r))
        await bot.reconcile()
        d = bot.last_decision
        if d:
            print(f"ผลตรวจ: {d.status} {d.reason} {json.dumps(d.info, ensure_ascii=False)}")
            for x in d.actions:
                print("  จะทำ:", bot.describe(x))
            if not d.actions and d.status == "ok":
                print("  ไม่ต้องทำอะไร — ออเดอร์ครบตามสูตร")
    except Halt as e:
        print("🛑", e)
    finally:
        await bot.c.close()


async def cmd_run(C, a):
    live = bool(a.live)
    if live and not C["live_allowed"]:
        sys.exit("config ยังไม่อนุญาต live (ตั้ง live_allowed = true)")
    bot, store = await build(C, live, a)
    if store.data["status"] == "closed":
        sys.exit("zone นี้ปิดไปแล้ว — จะไม่เปิดซ้ำ (ถ้าตั้งใจจริงให้ลบไฟล์สถานะเอง)")
    if store.data["status"] == "halted":
        if not a.clear_halt:
            sys.exit(f"zone ถูก halt: {store.data['halt_reason']} — ตรวจแล้วรันด้วย --clear-halt")
        store.data.update(status="active", halt_reason="")
        if live:
            store.save()
    res = await bot.run()
    print("จบ:", res)


async def cmd_close(C, a):
    bot, store = await build(C, bool(a.live), a)
    await bot.c.connect()
    try:
        await bot.close_zone("สั่งปิดจาก CLI")
    finally:
        await bot.c.close()


def cmd_tg():
    """อ่าน token จาก .env ข้างโปรเจกต์ (ไม่รับ token ทาง argument เพื่อไม่ให้ค้างใน shell history)"""
    import urllib.request, urllib.parse
    from .config import load_env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_env(os.path.join(root, ".env"))
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok:
        sys.exit("ยังไม่มี TG_BOT_TOKEN ใน .env")
    api = f"https://api.telegram.org/bot{tok}"
    with urllib.request.urlopen(api + "/getMe", timeout=10) as r:
        me = json.load(r)["result"]
    print(f"บอท Telegram: @{me.get('username')} ({me.get('first_name')})")
    if not chat:
        with urllib.request.urlopen(api + "/getUpdates", timeout=10) as r:
            ups = json.load(r)["result"]
        chats = {}
        for u in ups:
            c = (u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat")
            if c:
                chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
        if not chats:
            sys.exit(f"ยังไม่เห็นข้อความ — เปิด Telegram ทัก @{me.get('username')} (พิมพ์ /start) แล้วรันคำสั่งนี้อีกครั้ง")
        for cid, nm in chats.items():
            print(f"  chat id {cid}  ({nm})")
        print("ใส่ค่านี้ใน .env เป็น TG_CHAT_ID= แล้วรัน tg อีกครั้งเพื่อส่งข้อความทดสอบ")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": "✅ RBSI Grid Bot เชื่อม Telegram สำเร็จ"}).encode()
    with urllib.request.urlopen(urllib.request.Request(api + "/sendMessage", data=body), timeout=10) as r:
        ok = json.load(r).get("ok")
    print("ส่งข้อความทดสอบแล้ว" if ok else "ส่งไม่สำเร็จ")


def main():
    p = argparse.ArgumentParser(prog="rbsi_bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tg")
    u = sub.add_parser("ui")
    u.add_argument("--port", type=int, default=8777)
    u.add_argument("--no-open", action="store_true", help="ไม่เปิดเบราว์เซอร์ให้")
    for name in ("plan", "check", "run", "close", "status", "reopen"):
        s = sub.add_parser(name)
        s.add_argument("-c", "--config", required=True)
        if name == "plan":
            s.add_argument("--price", type=float)
            s.add_argument("--m", type=int)
            s.add_argument("--btc", type=float, help="Fixed Value: จำนวน BTC ที่ถือ (ไม่ใส่ = พอดีเป้า)")
            s.add_argument("--usdc", type=float, help="Fixed Value: USDC สำรอง")
            s.add_argument("--levels", type=int, default=5)
        if name in ("run", "close"):
            s.add_argument("--live", action="store_true")
        if name in ("run", "check"):
            s.add_argument("--adopt-manual", action="store_true", help="รับออเดอร์มือที่ตรงสูตรเป็นของบอท ยกเลิกตัวที่ไม่ตรง")
        if name == "run":
            s.add_argument("--force", action="store_true")
            s.add_argument("--clear-halt", action="store_true")
        if name in ("run", "check"):
            s.add_argument("--rebalance-now", action="store_true", help="Fixed Value: ยืนยันให้ปรับมูลค่าเข้าเป้าตอนเริ่ม")
            s.add_argument("--accept-balance", action="store_true", help="Fixed Value: ยอมรับยอด BTC ที่เปลี่ยนนอกบอท")
    a = p.parse_args()
    if a.cmd == "tg":
        return cmd_tg()
    if a.cmd == "ui":
        from .ui import serve
        return serve(a.port, not a.no_open)
    from .config import is_fv, is_kzm
    if is_fv(a.config) or is_kzm(a.config):          # บอท spot: Fixed Value ([fixed_value]) / KZM Grid ([kzm])                            # บอท Fixed Value (ไฟล์มีหมวด [fixed_value])
        from .fvcli import main as fv_main
        return fv_main(a, setup_log)
    C = load(a.config)
    if a.cmd == "plan":
        return cmd_plan(C, a)
    if a.cmd == "reopen":
        z = C["zone"]
        store = Store(C["state_dir"], z.zone_id)
        try:
            store.lock(z.zone_id)
        except LockError as e:
            sys.exit(f"{e} — หยุดบอทก่อน")
        try:
            dst = store.archive_closed()
        except ValueError as e:
            sys.exit(str(e))
        print(f"เปิด zone {z.zone_id} ใหม่แล้ว · สถานะเก่าเก็บไว้ที่ {dst}")
        print("ขั้นต่อไป: check / run ด้วย --adopt-manual → บอทรับช่วงจาก position และออเดอร์ที่อยู่บน Deribit ตอนนี้")
        return
    if a.cmd == "status":
        import fcntl, time
        z = C["zone"]
        st = Store(C["state_dir"], z.zone_id).data
        lp = os.path.join(os.path.expanduser(C["state_dir"]), f"{z.zone_id}.lock")
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
        hb = st.get("heartbeat") or {}
        print(f"process: {'🟢 กำลังรัน' if running else '⚪ ไม่ได้รัน'} · สถานะ zone: {st.get('status')}" + (f" ({st.get('halt_reason')})" if st.get('halt_reason') else ""))
        if hb:
            age = time.time() - hb["ts"]
            print(f"ตรวจล่าสุด {age:,.0f} วิที่แล้ว{' ⚠️ นานผิดปกติ' if running and age > 120 else ''} · ราคา {hb['mark']:,.1f} · position {hb['position']:+,.0f} "
                  f"· ชั้นที่ {z.word} {len(hb.get('levels_short') or [])}/{z.n} · ออเดอร์ของบอท {hb['own_orders']}/{hb.get('want_orders', z.n)} · ความเสี่ยงรวม {hb.get('exposure') or 0:,.0f}/{z.cap:,}")
        fills = st.get("fills") or []
        for f in fills[-5:]:
            print(f"  fill {time.strftime('%m-%d %H:%M', time.localtime(f['ts'] / 1000))} {f['side'].upper():<4} {f['price']:>10,.1f} × {f['amount']:,.0f} ชั้น {f['k']}")
        print(f"log: {os.path.join(os.path.expanduser(C['state_dir']), z.zone_id + '.log')}")
        return
    setup_log(C["state_dir"], C["zone"].zone_id)
    asyncio.run({"check": cmd_check, "run": cmd_run, "close": cmd_close}[a.cmd](C, a))


if __name__ == "__main__":
    main()
