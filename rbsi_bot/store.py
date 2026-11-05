# -*- coding: utf-8 -*-
"""สถานะถาวรของ zone (JSON เขียนแบบ atomic) + lock กันรันบอทซ้อน
หมายเหตุ: position/ออเดอร์ อ่านจาก exchange ทุกครั้ง — ไฟล์นี้เก็บเฉพาะสิ่งที่ exchange ไม่รู้:
  สถานะ zone (active/full/halted/closed), baseline ตอนรับช่วง, บัญชีไม้รายชั้น (ledger), ออเดอร์ที่บอทรู้จัก"""
from __future__ import annotations
import fcntl, hashlib, json, os, time

STATUSES = ("new", "active", "halted", "closed")


class LockError(Exception):
    pass


class Store:
    def __init__(self, state_dir: str, zone_id: str):
        self.dir = os.path.expanduser(state_dir)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, f"{zone_id}.json")
        self.zone_id = zone_id
        self._locks = []
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {"zone_id": self.zone_id, "status": "new", "seen_full": False, "halt_reason": "",
                "adopted_at": None, "ledger_start_ms": None, "baseline": {},
                "ledger": {"nets": {}, "by_order": {}, "trade_ids": [], "last_ts": 0, "last_sell_px": {}},
                "known_orders": {}, "fills": [], "created": int(time.time() * 1000)}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def archive_closed(self) -> str:
        """เปิด zone ที่ปิดแล้วใหม่ (กรอบ/gap/ขนาดเดิม): ย้ายไฟล์สถานะเก่าไปเป็น <zone_id>.closed-<เวลา>.json (ไม่ลบ)
        แล้วเริ่มสถานะ new → รันครั้งถัดไปรับช่วงจาก position/ออเดอร์บน exchange ใหม่ทั้งหมด (ใช้ --adopt-manual ได้)"""
        if self.data["status"] != "closed":
            raise ValueError(f"zone นี้สถานะ {self.data['status']} ไม่ใช่ closed — เปิดใหม่ได้เฉพาะ zone ที่ปิดแล้ว")
        dst = self.path[:-5] + time.strftime(".closed-%Y%m%d-%H%M%S.json")
        os.replace(self.path, dst)
        self.data = self._load()
        return dst

    def lock(self, name: str):
        """flock แบบไม่รอ — ล็อกทั้งราย zone และราย API key (กันสองบอทใช้ key/subaccount เดียวกัน)"""
        p = os.path.join(self.dir, f"{name}.lock")
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockError(f"มีบอทอีกตัวถือ lock '{name}' อยู่")
        self._locks.append(fd)

    def unlock(self):
        for fd in self._locks:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._locks = []

    @staticmethod
    def key_lock_name(client_id: str) -> str:
        return "key-" + hashlib.sha256((client_id or "none").encode()).hexdigest()[:12]

    # ---------- ledger ----------
    def nets(self) -> dict:
        """ยอดที่ถือสุทธิรายชั้น (short หรือ long ตามทิศ zone) = baseline + fill ที่ติด label ของบอท"""
        out = {int(k): float(v) for k, v in self.data["baseline"].items()}
        for k, v in self.data["ledger"]["nets"].items():
            out[int(k)] = out.get(int(k), 0.0) + float(v)
        return out

    def add_trade(self, t: dict, k: int, open_side: str = "sell") -> bool:
        """open_side = ฝั่งที่เพิ่มยอดถือของชั้น (short zone: sell · long zone: buy)"""
        L = self.data["ledger"]
        if t["trade_id"] in L["trade_ids"]:
            return False
        amt = float(t["amount"]) * (1 if t["direction"] == open_side else -1)
        L["nets"][str(k)] = float(L["nets"].get(str(k), 0.0)) + amt
        L["by_order"][t["order_id"]] = float(L["by_order"].get(t["order_id"], 0.0)) + float(t["amount"])
        L["trade_ids"].append(t["trade_id"])
        L["trade_ids"] = L["trade_ids"][-5000:]
        L["last_ts"] = max(L["last_ts"], int(t["timestamp"]))
        if t["direction"] == open_side:                                 # ราคา/fee ของไม้เปิดล่าสุด (ไว้คิดกำไรตอนคู่ปิด)
            L.setdefault(f"last_{open_side}_px", {})[str(k)] = float(t["price"])
            if t.get("fee") is not None and float(t["amount"]) > 0:      # fee ต่อ USD ของไม้เปิด
                L.setdefault(f"last_{open_side}_fee", {})[str(k)] = float(t["fee"]) / float(t["amount"])
        L.setdefault("last_side", {})[str(k)] = t["direction"]
        return True

    def last_side(self) -> dict:
        return {int(k): v for k, v in self.data["ledger"].get("last_side", {}).items()}

    def order_filled_in_ledger(self, order_id: str) -> float:
        return float(self.data["ledger"]["by_order"].get(order_id, 0.0))

    def log_fill(self, entry: dict):
        self.data["fills"].append(entry)
        self.data["fills"] = self.data["fills"][-500:]
