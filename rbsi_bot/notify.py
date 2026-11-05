# -*- coding: utf-8 -*-
"""แจ้งเตือน Telegram (Bot API sendMessage) — ไม่ตั้ง token = พิมพ์ลง log อย่างเดียว"""
from __future__ import annotations
import asyncio, json, logging, threading, time, urllib.parse, urllib.request

log = logging.getLogger("notify")


async def _daemon_call(fn, *args):
    """เหมือน asyncio.to_thread แต่รันใน daemon thread — long-poll getUpdates (ค้างได้ 25 วิ) ไม่ขวาง process ตอนหยุดบอท
    (to_thread ใช้ executor กลางของ asyncio ซึ่ง asyncio.run รอ thread จบก่อนปิด → กดหยุดแล้วรอได้ถึง ~25 วิ)"""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def done(cb, v):
        if not fut.done():
            cb(v)

    def work():
        try:
            r = fn(*args)
        except Exception as e:
            r, cb = e, fut.set_exception
        else:
            cb = fut.set_result
        try:
            loop.call_soon_threadsafe(done, cb, r)
        except RuntimeError:                          # loop ปิดไปแล้ว (บอทหยุดระหว่างรอ) — ทิ้งผล
            pass

    threading.Thread(target=work, daemon=True).start()
    return await fut


class Notifier:
    def __init__(self, token: str | None, chat_id: str | None, prefix: str = "", dedupe_sec: float = 300):
        self.token, self.chat_id, self.prefix = token, chat_id, prefix
        self.dedupe_sec = dedupe_sec
        self._last: dict[str, float] = {}
        self._q: asyncio.Queue | None = None
        self._worker = None

    def _post(self, text: str):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10) as r:
            return json.load(r)

    def _get_updates(self, offset: int, timeout: int):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?" + urllib.parse.urlencode({"offset": offset, "timeout": timeout})
        with urllib.request.urlopen(url, timeout=timeout + 10) as r:
            return json.load(r).get("result", [])

    async def poll(self, timeout: int = 25) -> list[str]:
        """ข้อความใหม่จาก chat ที่ตั้งไว้เท่านั้น (คนอื่นทักบอทจะไม่ถูกนับ)"""
        if not (self.token and self.chat_id):
            await asyncio.sleep(timeout)
            return []
        off = getattr(self, "_offset", 0)
        try:
            ups = await _daemon_call(self._get_updates, off, timeout)
        except Exception as e:
            log.debug("getUpdates: %s", e)
            await asyncio.sleep(5)
            return []
        out = []
        for u in ups:
            self._offset = max(off, u["update_id"] + 1)
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) == str(self.chat_id) and m.get("text"):
                out.append(m["text"].strip())
        return out

    async def send(self, text: str, key: str | None = None):
        """key = กันข้อความเดิมซ้ำภายใน dedupe_sec (เช่น เตือน margin)"""
        now = time.time()
        if key is not None:
            if now - self._last.get(key, 0) < self.dedupe_sec:
                return
            self._last[key] = now
        msg = f"{self.prefix}{text}"
        log.info("NOTIFY %s", msg.replace("\n", " | "))
        if not (self.token and self.chat_id):
            return
        try:
            await asyncio.to_thread(self._post, msg)
        except Exception as e:
            log.warning("ส่ง Telegram ไม่สำเร็จ: %s", e)

    async def send_raw(self, text: str):
        """ส่งโดยไม่ใส่ชื่อบอทนำหน้า (ข้อความรวมหลายบอท เช่น /status)"""
        pre, self.prefix = self.prefix, ""
        try:
            await self.send(text)
        finally:
            self.prefix = pre

    def post(self, text: str, key: str | None = None):
        """ส่งแบบไม่รอ — ใช้ในเส้นทางยิงออเดอร์ (Telegram ช้า/ค้างไม่ทำให้ออเดอร์ช้าตาม)
        คิวเดียวส่งทีละข้อความ ลำดับข้อความจึงไม่สลับกัน"""
        if self._q is None:
            self._q = asyncio.Queue()
            self._worker = asyncio.get_running_loop().create_task(self._drain())
        self._q.put_nowait((text, key))

    async def _drain(self):
        while True:
            text, key = await self._q.get()
            try:
                await self.send(text, key)
            except Exception as e:
                log.warning("ส่ง Telegram ไม่สำเร็จ: %s", e)
            finally:
                self._q.task_done()

    async def flush(self, timeout: float = 15):
        """รอข้อความในคิวส่งให้หมด (ก่อนข้อความสำคัญ เช่น HALT และก่อนจบ process)"""
        if self._q is not None:
            try:
                await asyncio.wait_for(self._q.join(), timeout)
            except asyncio.TimeoutError:
                log.warning("ส่ง Telegram ค้างในคิว %d ข้อความ", self._q.qsize())
