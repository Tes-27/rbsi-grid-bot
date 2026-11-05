# -*- coding: utf-8 -*-
"""Telegram /status รวมทุกบอท — ดึงข้อความตัวเดียว (lock) · ตอบสถานะทุกตัวจากไฟล์ (rbsi_bot/tghub.py)"""
import asyncio, os, time
import pytest
from rbsi_bot import tghub
from rbsi_bot.tghub import write_status, combined, Leader
from rbsi_bot.bot import GridBot, Safety
from rbsi_bot.store import Store
from rbsi_bot.notify import Notifier
from tests.fake_exchange import FakeExchange
from tests.test_bot_sim import Z, ZL, seed_manual


def test_combined_running_stale_stopped(tmp_path):
    sd, now = str(tmp_path), time.time()
    write_status(sd, "a", "HFT-1", "position +1,190", "active", now=now)
    write_status(sd, "b", "Beta2", "position -2,000", "active", now=now - 600)                 # process อยู่แต่ไม่อัปเดต 10 นาที
    write_status(sd, "c", "Rebalance", "x", "halted", now=now - 60)
    write_status(sd, "d", "old", "x", "active", now=now - 5 * 86400)
    p = os.path.join(sd, "tgstatus", "d.json"); s = open(p).read().replace(f'"pid": {os.getpid()}', '"pid": 999999'); open(p, "w").write(s)
    msg = "\n".join(combined(sd, now=now))
    assert "🟢 HFT-1\nposition +1,190" in msg
    assert "⚠️ Beta2 · ไม่อัปเดตมา 10 นาที" in msg and "position -2,000" in msg
    assert "⚪ Rebalance · ไม่ได้รัน (🛑 halt)" in msg
    assert "old" not in msg                                                                       # ไม่ได้รันนานเกิน 3 วัน → ไม่แสดง
    assert msg.startswith("📋 สถานะบอท 2 ตัวที่รันอยู่ / ทั้งหมด 3")
    one = "\n".join(combined(sd, "hft-1", now=now))
    assert "HFT-1" in one and "Beta2" not in one
    assert combined(sd, "nope", now=now) == ["ไม่พบบอทชื่อ 'nope'"]


def test_long_status_split_under_telegram_limit(tmp_path):
    for i in range(12):
        write_status(str(tmp_path), f"z{i}", f"Z{i:02d}", "บรรทัด " * 120, "active")
    msgs = combined(str(tmp_path))
    assert len(msgs) > 1 and all(len(m) <= 4096 for m in msgs)
    assert sum(m.count("🟢 Z") for m in msgs) == 12


def test_leader_lock_exclusive_and_released(tmp_path):
    a, b = Leader(str(tmp_path)), Leader(str(tmp_path))
    assert a.try_acquire() and not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


class SharedTG:
    """Telegram จริง: ข้อความหนึ่งไปถึงผู้ดึงได้ตัวเดียว · จดว่าใครดึง"""
    def __init__(self):
        self.inbox, self.pollers, self.sent = [], [], []


class FakeTG(Notifier):
    def __init__(self, hub, name):
        super().__init__(None, None, prefix=f"[{name}] ")
        self.hub, self.name = hub, name
    async def poll(self, timeout=25):
        await asyncio.sleep(0.001)
        self.hub.pollers.append(self.name)
        out, self.hub.inbox = self.hub.inbox, []
        return out
    async def send(self, text, key=None):
        self.hub.sent.append(self.prefix + text)


def test_one_poller_answers_for_all_and_handover(tmp_path, monkeypatch):
    monkeypatch.setattr(tghub, "LEADER_RETRY", 0.01)

    async def run():
        hub = SharedTG()
        bots = []
        for z, name, m in ((Z, "Beta2", 3), (ZL, "HFT-1", 9)):
            ex = FakeExchange(83458)
            seed_manual(ex, m, 83458, z=z)
            store = Store(str(tmp_path), z.zone_id)
            bot = GridBot(z, ex, store, FakeTG(hub, name), live=True, safety=Safety(settle_sec=1.0, adopt_manual=True, summary_hours=0),
                          sleep=ex.sleep, now=ex.now)
            await bot.adopt(); await bot.reconcile()
            bots.append(bot)
        hub.sent.clear()
        tasks = [asyncio.create_task(b.tg_loop()) for b in bots]
        await asyncio.sleep(0.05)
        assert len(set(hub.pollers)) == 1                                       # ดึงข้อความตัวเดียว
        first = hub.pollers[0]
        hub.inbox.append("/status")
        await asyncio.sleep(0.05)
        assert len(hub.sent) == 1
        msg = hub.sent[0]
        assert msg.startswith("📋 สถานะบอท 2 ตัว") and "🟢 Beta2" in msg and "🟢 HFT-1" in msg   # ทั้งสองตัวในข้อความเดียว ไม่มีชื่อบอทนำหน้า
        assert "position -3,000" in msg and "position +9,000" in msg
        hub.inbox.append("/status hft-1")
        await asyncio.sleep(0.05)
        assert "HFT-1" in hub.sent[1] and "Beta2" not in hub.sent[1]
        # ตัวที่ดึงข้อความหยุด → อีกตัวรับหน้าที่ · /status บอกว่าตัวที่หยุด "ไม่ได้รัน"
        leader = next(b for b in bots if b.n.name == first)
        other = next(b for b in bots if b is not leader)
        leader.store.data["status"] = "closed"
        await asyncio.wait_for(tasks[bots.index(leader)], 1)
        await asyncio.sleep(0.1)
        assert hub.pollers[-1] == other.n.name
        hub.sent.clear(); hub.inbox.append("/status")
        await asyncio.sleep(0.05)
        assert f"⚪ {first} · ไม่ได้รัน" in hub.sent[0] and f"🟢 {other.n.name}" in hub.sent[0]
        other.store.data["status"] = "closed"
        await asyncio.wait_for(tasks[bots.index(other)], 1)
    asyncio.run(run())


# ---------- long-poll ไม่ขวางการหยุดบอท ----------
def test_daemon_call_result_and_error():
    from rbsi_bot.notify import _daemon_call
    def boom():
        raise ValueError("x")
    async def go():
        assert await _daemon_call(lambda a, b: a + b, 2, 3) == 5
        with pytest.raises(ValueError):
            await _daemon_call(boom)
    asyncio.run(go())


def test_sigint_during_long_poll_exits_fast(tmp_path):
    """เดิม getUpdates รันผ่าน asyncio.to_thread → asyncio.run รอ thread (ค้างได้ 25 วิ) ก่อนปิด → กดหยุดจากหน้าเว็บช้า ~20 วิ"""
    import signal, subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = tmp_path / "poller.py"
    src.write_text(f"""import asyncio, sys, time
sys.path.insert(0, {root!r})
from rbsi_bot.notify import _daemon_call
async def main():
    print("ready", flush=True)
    await _daemon_call(time.sleep, 30)
asyncio.run(main())
""")
    p = subprocess.Popen([sys.executable, str(src)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    assert p.stdout.readline().strip() == "ready"
    time.sleep(0.3)
    t0 = time.time()
    p.send_signal(signal.SIGINT)
    p.wait(timeout=10)
    assert time.time() - t0 < 3
