# -*- coding: utf-8 -*-
"""Telegram /status รวมทุกบอท (9 ต.ค. 2026)

ปัญหาเดิม: ทุกบอทใช้ Telegram bot token เดียวกันและต่างตัวต่างเรียก getUpdates — ข้อความหนึ่งไปถึงบอทได้ตัวเดียว
(ตัวที่ดึงก่อน "รับทราบ" แล้ว Telegram ลบออก · ดึงพร้อมกัน = 409 Conflict) → /status ตอบแค่บางตัวแบบสุ่ม
แก้:
  1) ทุกบอทเขียนข้อความสถานะของตัวเองลง <state_dir>/tgstatus/<id>.json (pid, เวลา) ทุกรอบของ tg_loop
  2) บอทที่ถือ lock "telegram-poll" ได้เป็นตัวเดียวที่ดึงข้อความ · ตัวอื่นลองชิง lock ทุก LEADER_RETRY วิ
     (ตัวที่ถือหยุด/ตาย → OS ปล่อย lock เอง → ตัวถัดไปรับหน้าที่)
  3) /status = รวมสถานะทุกบอทจากไฟล์ · /status <ชื่อ> = เฉพาะตัว · บอทที่ pid ไม่อยู่แล้ว = "ไม่ได้รัน"
ข้อความแจ้งเตือนปกติ (fill, halt, zone เต็ม, สรุปตามรอบ) ยังส่งจากแต่ละบอทเหมือนเดิม"""
from __future__ import annotations
import asyncio, fcntl, json, os, time

STATUS_DIR = "tgstatus"
LEADER_LOCK = "telegram-poll.lock"
LEADER_RETRY = 10            # วินาที — บอทที่ไม่ได้เป็นผู้ดึงข้อความ ลองชิง lock + เขียนสถานะทุกเท่านี้
STALE_SEC = 120              # สถานะเก่ากว่านี้ทั้งที่ process ยังอยู่ = เตือนว่าอาจค้าง
FORGET_SEC = 3 * 86400       # บอทที่ไม่ได้รันและไม่อัปเดตเกินนี้ ไม่แสดงใน /status
TG_LIMIT = 3900              # Telegram รับข้อความได้ 4096 ตัวอักษร


def _dir(state_dir: str) -> str:
    d = os.path.join(os.path.expanduser(state_dir), STATUS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def write_status(state_dir: str, bot_id: str, name: str, text: str, status: str, now: float | None = None):
    p = os.path.join(_dir(state_dir), f"{bot_id}.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": bot_id, "name": name, "text": text, "status": status, "pid": os.getpid(),
                   "ts": time.time() if now is None else now}, f, ensure_ascii=False)
    os.replace(tmp, p)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_all(state_dir: str) -> list[dict]:
    out = []
    d = _dir(state_dir)
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
    return out


def combined(state_dir: str, query: str = "", now: float | None = None) -> list[str]:
    """ข้อความตอบ /status (แบ่งเป็นหลายข้อความถ้ายาวเกิน Telegram)"""
    now = time.time() if now is None else now
    q = query.strip().lower()
    blocks = []
    for s in sorted(read_all(state_dir), key=lambda s: (s.get("name") or s.get("id") or "").lower()):
        name = s.get("name") or s.get("id")
        if q and q not in name.lower() and q not in str(s.get("id", "")).lower():
            continue
        age = now - float(s.get("ts") or 0)
        alive = _alive(s.get("pid")) and s.get("status") not in ("halted", "closed")
        when = time.strftime("%d/%m %H:%M", time.localtime(float(s.get("ts") or 0)))
        if not alive:
            if age > FORGET_SEC and not q:
                continue
            st = {"halted": "🛑 halt", "closed": "✅ zone ปิดแล้ว"}.get(s.get("status"), "หยุดอยู่")
            blocks.append(f"⚪ {name} · ไม่ได้รัน ({st}) · อัปเดตล่าสุด {when}")
        elif age > STALE_SEC:
            blocks.append(f"⚠️ {name} · ไม่อัปเดตมา {age / 60:,.0f} นาที (process ยังอยู่ — อาจค้าง)\n{s.get('text', '')}")
        else:
            blocks.append(f"🟢 {name}\n{s.get('text', '')}")
    if not blocks:
        return [f"ไม่พบบอทชื่อ '{query}'" if q else "ไม่พบสถานะบอท"]
    head = f"📋 สถานะบอท {sum(1 for b in blocks if not b.startswith('⚪'))} ตัวที่รันอยู่ / ทั้งหมด {len(blocks)}"
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > TG_LIMIT:
            msgs.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    return msgs + [cur]


class Leader:
    """lock ของผู้ดึงข้อความ Telegram (flock ไม่รอ · ปล่อยเองเมื่อ process จบ)"""
    def __init__(self, state_dir: str):
        self.path = os.path.join(os.path.expanduser(state_dir), LEADER_LOCK)
        self.fd = None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def bot_name(bot) -> str:
    return (bot.n.prefix or "").strip().strip("[]").strip() or bot.store.zone_id


async def tg_loop(bot):
    """ใช้แทน tg_loop เดิมของ GridBot / FVBot / KZMBot (ต้องมี n, store, status_text(), sf.summary_hours, last_summary, now)"""
    sd, bid, name = bot.store.dir, bot.store.zone_id, bot_name(bot)
    leader = Leader(sd)
    try:
        while bot.store.data["status"] not in ("halted", "closed"):
            write_status(sd, bid, name, bot.status_text(), bot.store.data["status"])
            if not leader.fd and leader.try_acquire():
                await bot.n.poll(0)                       # เพิ่งได้หน้าที่ — ข้ามข้อความเก่าที่ค้าง ไม่ตอบย้อนหลัง
                write_status(sd, bid, name, bot.status_text(), bot.store.data["status"])
            if leader.fd:
                for text in await bot.n.poll(25):
                    cmd, _, arg = text.strip().partition(" ")
                    cmd = cmd.lower().lstrip("/").split("@")[0]
                    if cmd in ("status", "สถานะ"):
                        write_status(sd, bid, name, bot.status_text(), bot.store.data["status"])
                        for m in combined(sd, arg):
                            await bot.n.send_raw(m)
                    elif cmd in ("help", "start"):
                        await bot.n.send_raw("คำสั่ง: /status = สถานะบอททุกตัว · /status <ชื่อ> เช่น /status HFT-1 = เฉพาะตัว")
            else:
                await asyncio.sleep(LEADER_RETRY)
            if bot.sf.summary_hours and bot.now() - bot.last_summary >= bot.sf.summary_hours * 3600:
                if bot.last_summary:
                    await bot.n.send("📋 สรุปตามรอบ\n" + bot.status_text())
                bot.last_summary = bot.now()
    finally:
        try:
            write_status(sd, bid, name, bot.status_text(), bot.store.data["status"])
        finally:
            leader.release()                              # ให้บอทตัวอื่นรับหน้าที่ดึงข้อความต่อ
