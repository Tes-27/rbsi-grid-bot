# -*- coding: utf-8 -*-
"""Deribit WebSocket JSON-RPC client (asyncio) — โครงเดียวกับ createDeribitConn ใน deribit-monitor
heartbeat 30 วิ · auth client_credentials + ต่ออายุด้วย refresh_token · subscription callback"""
from __future__ import annotations
import asyncio, json, logging, time
import websockets

log = logging.getLogger("deribit")


class RpcError(Exception):
    def __init__(self, method, err):
        self.code = err.get("code")
        self.data = err.get("data")
        super().__init__(f"{method}: {err.get('message')} ({self.code}) {self.data or ''}".strip())


class RpcTimeout(Exception):
    pass


class DeribitClient:
    def __init__(self, testnet: bool, client_id: str | None, client_secret: str | None, timeout: float = 10.0, on_event=None):
        self.url = "wss://test.deribit.com/ws/api/v2" if testnet else "wss://www.deribit.com/ws/api/v2"
        self.cid, self.secret, self.timeout = client_id, client_secret, timeout
        self.on_event = on_event
        self.ws = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = None
        self._refresh = None
        self.closed = asyncio.Event()

    async def connect(self):
        self.closed.clear()
        self.ws = await websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2 ** 22)
        self._reader = asyncio.create_task(self._read())
        await self.rpc("public/set_heartbeat", {"interval": 30})
        if self.cid:
            await self._auth({"grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.secret})

    async def _auth(self, params):
        r = await self.rpc("public/auth", params)
        if self._refresh:
            self._refresh.cancel()
        self._refresh = asyncio.create_task(self._refresh_later(r["refresh_token"], r["expires_in"]))

    async def _refresh_later(self, token, expires_in):
        await asyncio.sleep(max(expires_in - 120, 60))
        try:
            await self._auth({"grant_type": "refresh_token", "refresh_token": token})
        except Exception as e:                         # ถ้าต่ออายุไม่ได้ ปิดสายให้ลูปหลัก reconnect ใหม่
            log.warning("refresh token ล้มเหลว: %s", e)
            await self.close()

    async def _read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                    continue
                if msg.get("method") == "heartbeat":
                    if (msg.get("params") or {}).get("type") == "test_request":
                        asyncio.create_task(self._send("public/test", {}))
                    continue
                if msg.get("method") == "subscription" and self.on_event:
                    try:
                        self.on_event(msg["params"])
                    except Exception:
                        log.exception("on_event")
        except Exception as e:
            log.warning("สายหลุด: %s", e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            self._pending.clear()
            self.closed.set()

    async def _send(self, method, params):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}))
        return mid

    async def rpc(self, method: str, params: dict | None = None, timeout: float | None = None):
        if self.ws is None or self.closed.is_set():
            raise ConnectionError("not connected")
        fut = asyncio.get_running_loop().create_future()
        self._id += 1
        mid = self._id
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}))
        try:
            msg = await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise RpcTimeout(f"{method} timeout")
        if "error" in msg:
            raise RpcError(method, msg["error"])
        return msg["result"]

    async def close(self):
        if self._refresh:
            self._refresh.cancel()
        if self.ws is not None:
            await self.ws.close()
        self.closed.set()

    # ---------- wrappers ----------
    async def subscribe(self, channels):
        return await self.rpc("private/subscribe", {"channels": channels})

    async def ticker(self, instrument):
        return await self.rpc("public/ticker", {"instrument_name": instrument})

    async def position(self, instrument):
        return await self.rpc("private/get_position", {"instrument_name": instrument})

    async def open_orders(self, instrument):
        return await self.rpc("private/get_open_orders_by_instrument", {"instrument_name": instrument, "type": "all"})

    async def account_summary(self, currency="BTC"):
        return await self.rpc("private/get_account_summary", {"currency": currency})

    async def order_state(self, order_id):
        return await self.rpc("private/get_order_state", {"order_id": order_id})

    async def trades_since(self, instrument, start_ms: int):
        """fill ของบัญชีนี้ตั้งแต่ start_ms (เรียงเก่า→ใหม่) ดึงต่อจนหมด"""
        out, start = [], start_ms
        end = int(time.time() * 1000) + 60_000
        for _ in range(50):
            r = await self.rpc("private/get_user_trades_by_instrument_and_time",
                               {"instrument_name": instrument, "start_timestamp": start, "end_timestamp": end,
                                "count": 1000, "sorting": "asc"})
            tr = r.get("trades", [])
            out += tr
            if not r.get("has_more") or not tr:
                break
            start = tr[-1]["timestamp"]
        return out

    async def place(self, side, instrument, amount, *, price=None, label, reduce_only=False, post_only=True):
        """limit post-only เท่านั้น (ถ้าจะกลายเป็น taker Deribit ปฏิเสธแทนการจับคู่)"""
        if price is None or not post_only:
            raise ValueError("บอทนี้ส่งได้เฉพาะ limit post-only")
        p = {"instrument_name": instrument, "amount": amount, "label": label,
             "type": "limit", "price": price, "post_only": True, "reject_post_only": True, "time_in_force": "good_til_cancelled"}
        if reduce_only:                                  # ส่งเฉพาะตอนใช้ (spot ไม่มีแนวคิด reduce-only)
            p["reduce_only"] = True
        return await self.rpc(f"private/{side}", p, timeout=max(self.timeout, 15.0))   # เครื่องจับคู่อาจช้าตอนตลาดวุ่น

    async def cancel(self, order_id):
        return await self.rpc("private/cancel", {"order_id": order_id})
