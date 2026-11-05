# -*- coding: utf-8 -*-
"""หน้าเว็บควบคุมบอท (รันในเครื่องเท่านั้น 127.0.0.1) — `python -m rbsi_bot ui` หรือดับเบิลคลิก rbsi-ui.command
  • อ่าน key จาก .env แล้วดึงบัญชีจริงให้เอง (อ่านอย่างเดียว) — key ไม่ถูกส่งไปที่เบราว์เซอร์
  • หลาย subaccount: key ชุดละ suffix ใน .env (DERIBIT_CLIENT_ID_<SUB> / DERIBIT_CLIENT_SECRET_<SUB> / DERIBIT_NAME_<SUB>)
    ไม่มี suffix = บัญชีหลักเดิม · เพิ่ม key จากหน้าเว็บได้ (ทดสอบ login ก่อนบันทึก · secret ไม่ถูกส่งกลับไปที่เบราว์เซอร์)
  • สร้าง/แก้/ลบไฟล์ zone ใน zones/ (ไฟล์เดิมย้ายไป zones/.bak/ ก่อนเขียนทับหรือลบ — กู้คืนได้)
  • สั่ง check / เริ่ม LIVE / หยุด / ปิด zone — เรียก CLI ตัวเดิม (`python -m rbsi_bot ...`) ด่านความปลอดภัยเดิมทั้งหมดยังอยู่
ทุก /api ต้องมี token ที่ฝังในหน้า (กันเว็บอื่นในเบราว์เซอร์ยิงเข้ามา) และ Host ต้องเป็นเครื่องนี้"""
from __future__ import annotations
import asyncio, json, math, os, re, secrets, signal, subprocess, sys, threading, time, tomllib, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .grid import ZoneConfig, fmtnum
from .config import load, load_env
from .store import Store
from .deribit import DeribitClient
from .transfer import transferable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZONES = os.path.join(ROOT, "zones")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENV_PATH = os.path.join(ROOT, ".env")
ACCT_RE = re.compile(r"^DERIBIT_CLIENT_ID(?:_([A-Z0-9_]+))?$")
SAFETY_DEF = {"tol_frac": 0.25, "max_catchup": 2, "chase_frac": 0.05, "liq_buffer": 500, "default_mmr": 0.0126,
              "settle_sec": 0.3, "debounce_sec": 0.1, "reconcile_sec": 30, "pending_ttl": 60, "flux_halt_sec": 180,
              "max_orders_per_cycle": 6, "order_spacing_sec": 0.15, "throttle_max_sec": 30, "heartbeat_sec": 300, "summary_hours": 12}


# ---------- ไฟล์ zone ----------
def zone_path(file: str) -> str:
    if not FILE_RE.match(file or "") or file.startswith("."):
        raise ValueError("ชื่อไฟล์ใช้ได้แค่ a-z 0-9 . _ -")
    return os.path.join(ZONES, file + ".toml")


def read_zone(file: str) -> dict:
    with open(zone_path(file), "rb") as f:
        c = tomllib.load(f)
    z, sf, d = c["zone"], {**SAFETY_DEF, **c.get("safety", {})}, c.get("deribit", {})
    m = ACCT_RE.match(d.get("client_id_env", "DERIBIT_CLIENT_ID"))
    return {"file": file, "name": c.get("name", ""), "acct": (m.group(1) or "") if m else "?" + d.get("client_id_env", ""), "lower": z["lower"], "upper": z["upper"], "gap": z["gap"], "size": z["size"],
            "direction": z.get("direction", "short"), "max_orders": z.get("max_orders", 90),
            "instrument": z.get("instrument", "BTC-PERPETUAL"), "live": bool(c.get("live_allowed", False)),
            "testnet": bool(d.get("testnet", False)), "state_dir": c.get("state_dir", "~/.rbsi-bot"), **sf}


def zone_cfg(f: dict) -> ZoneConfig:
    return ZoneConfig(instrument=f.get("instrument") or "BTC-PERPETUAL", lower=float(f["lower"]), upper=float(f["upper"]),
                      gap=float(f["gap"]), size=int(f["size"]), tol_frac=float(f["tol_frac"]),
                      max_catchup=int(f["max_catchup"]), chase_frac=float(f["chase_frac"]),
                      direction=str(f.get("direction") or "short"), max_orders=int(float(f.get("max_orders", 90))))


def zone_toml(f: dict) -> str:
    """สร้าง TOML จากค่าในฟอร์ม — ตรวจด้วย ZoneConfig ตัวเดียวกับบอท (ผิดกติกา = ValueError)"""
    f = {**SAFETY_DEF, "max_orders": 90, **f}
    for k in ("lower", "upper", "gap", "size", "max_orders", *SAFETY_DEF):
        v = float(f[k])
        if not math.isfinite(v):
            raise ValueError(f"{k} ต้องเป็นตัวเลข")
    if float(f["size"]) != int(float(f["size"])):
        raise ValueError("size ต้องเป็นจำนวนเต็ม")
    f["size"] = int(float(f["size"]))
    cfg = zone_cfg(f)
    if int(f["max_catchup"]) < 1:
        raise ValueError("max_catchup ต้อง ≥ 1")
    if not 0 < float(f["default_mmr"]) < 0.2:
        raise ValueError("MM rate ต้องอยู่ระหว่าง 0–20%")
    name = re.sub(r'["\\\n\r]', "", str(f.get("name") or ""))[:40]
    acct = str(f.get("acct") or "")
    if acct and not re.fullmatch(r"[A-Z0-9_]+", acct):
        raise ValueError("รหัสบัญชีไม่ถูกต้อง")
    id_env, secret_env = acct_env(acct)
    b = lambda x: "true" if x in (True, "true", 1) else "false"
    n = lambda k: fmtnum(float(f[k]))
    return f'''# สร้างจากหน้าเว็บ (rbsi_bot ui) · {time.strftime("%Y-%m-%d %H:%M")}
# ความลับ (API key, Telegram token) อยู่ใน ~/rbsi-grid-bot/.env (chmod 600) ไม่ใส่ในไฟล์นี้
name = "{name}"                      # ชื่อที่ขึ้นหน้าข้อความ Telegram
env_file = "../.env"               # ไฟล์ zone อยู่ใน zones/ → .env อยู่โฟลเดอร์บน
state_dir = "{f.get("state_dir") or "~/.rbsi-bot"}"
live_allowed = {b(f.get("live"))}                # ต้องเป็น true และสั่ง --live ถึงจะส่งคำสั่งจริง

[deribit]
testnet = {b(f.get("testnet"))}
client_id_env = "{id_env}"               # key ของ subaccount นี้ใน .env
client_secret_env = "{secret_env}"

[zone]                              # zone ย่อยเท่านั้น — การต่อพอร์ตทำเอง
instrument = "{cfg.instrument}"
direction = "{cfg.direction}"                # short = position 0 ที่กรอบล่าง · long = position 0 ที่กรอบบน
lower = {fmtnum(cfg.lower)}
upper = {fmtnum(cfg.upper)}
gap = {fmtnum(cfg.gap)}
size = {cfg.size}                          # USD ต่อชั้น · {cfg.n} ชั้น · เพดาน {cfg.word} {cfg.cap:,} USD
max_orders = {cfg.max_orders}                    # วางเฉพาะออเดอร์ที่ใกล้ราคาที่สุดเท่านี้ตัว (Deribit รับได้ 100 ตัวต่อสัญญา) · 0 = วางทุกชั้น

[safety]
tol_frac = {n("tol_frac")}
max_catchup = {int(float(f["max_catchup"]))}
chase_frac = {n("chase_frac")}
liq_buffer = {n("liq_buffer")}
default_mmr = {n("default_mmr")}
settle_sec = {n("settle_sec")}
debounce_sec = {n("debounce_sec")}
reconcile_sec = {n("reconcile_sec")}
pending_ttl = {n("pending_ttl")}
flux_halt_sec = {n("flux_halt_sec")}
max_orders_per_cycle = {int(float(f["max_orders_per_cycle"]))}
order_spacing_sec = {n("order_spacing_sec")}
throttle_max_sec = {n("throttle_max_sec")}
heartbeat_sec = {n("heartbeat_sec")}                 # log "ทำงานปกติ" ทุกกี่วินาที
summary_hours = {n("summary_hours")}                  # สรุปเข้า Telegram ทุกกี่ชั่วโมง (0 = ปิด)

[telegram]
token_env = "TG_BOT_TOKEN"
chat_id_env = "TG_CHAT_ID"
'''


# ---------- สถานะ process ----------
def lock_path(state_dir: str, zone_id: str) -> str:
    return os.path.join(os.path.expanduser(state_dir), f"{zone_id}.lock")


def lock_pids(path: str) -> list[int]:
    """PID ที่เปิดไฟล์ lock ค้างไว้ (= บอทที่รันอยู่ ไม่ว่าจะเริ่มจาก terminal หรือจากหน้าเว็บ)"""
    if not os.path.exists(path):
        return []
    r = subprocess.run(["lsof", "-t", path], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split() if x.isdigit() and int(x) != os.getpid()]


def is_locked(path: str) -> bool:
    import fcntl
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def pid_mode(pids: list[int]) -> str | None:
    """'live' / 'dry' จาก command line ของ process ที่ถือ lock"""
    if not pids:
        return None
    r = subprocess.run(["ps", "-o", "command=", "-p", ",".join(map(str, pids))], capture_output=True, text=True)
    return "live" if "--live" in r.stdout else "dry" if r.stdout.strip() else None


def crash_info(state_dir: str, lock_id: str) -> dict | None:
    """ช่วงล่าสุดของ <id>.out (output ของ process ที่เริ่มจากหน้าเว็บ) ถ้าจบด้วย error ไม่คาดคิด → คืน error ให้หน้าเว็บโชว์
    (หยุดจากปุ่ม = มีบรรทัด "หยุดจากหน้าเว็บ" ต่อท้าย · Ctrl+C = KeyboardInterrupt → ไม่นับ)"""
    p = os.path.join(os.path.expanduser(state_dir), f"{lock_id}.out")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fo:
        fo.seek(max(0, os.path.getsize(p) - 40_000))
        text = fo.read().decode("utf-8", errors="replace")
    last = text.rsplit("\n===== ", 1)[-1]
    if "Traceback" not in last or "KeyboardInterrupt" in last or "หยุดจากหน้าเว็บ" in last.split("\n", 1)[0]:
        return None
    lines = [x for x in last.splitlines() if x.strip()]
    return {"when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))), "error": lines[-1][:300] if lines else "",
            "tail": "\n".join(lines[-25:])}


def zone_state(f: dict) -> dict:
    cfg = zone_cfg(f)
    st = Store(f["state_dir"], cfg.zone_id).data
    lp = lock_path(f["state_dir"], cfg.zone_id)
    running = is_locked(lp)
    pids = lock_pids(lp) if running else []                  # lsof ช้า (~0.2 วิ) → เรียกครั้งเดียว
    return {"zone_id": cfg.zone_id, "n": cfg.n, "cap": cfg.cap, "status": st.get("status"), "halt_reason": st.get("halt_reason", ""),
            "running": running, "pids": pids, "heartbeat": st.get("heartbeat") or {},
            "fills": (st.get("fills") or [])[-10:], "nets": {str(k): v for k, v in Store(f["state_dir"], cfg.zone_id).nets().items()},
            "adopted_at": st.get("adopted_at"), "saved": os.path.exists(zone_path(f["file"])),
            "mode": pid_mode(pids) if running else None, "crash": None if running else crash_info(f["state_dir"], cfg.zone_id)}


def list_zones() -> list[dict]:
    out = []
    for fn in sorted(os.listdir(ZONES)) if os.path.isdir(ZONES) else []:
        if not fn.endswith(".toml"):
            continue
        try:
            f = read_zone(fn[:-5])
            out.append({**f, "state": zone_state(f)})
        except Exception as e:
            out.append({"file": fn[:-5], "error": str(e)})
    return out


# ---------- บัญชี Deribit / API key หลายชุด ----------
_acct_cache: dict = {}


def acct_env(acct: str):
    sfx = f"_{acct}" if acct else ""
    return f"DERIBIT_CLIENT_ID{sfx}", f"DERIBIT_CLIENT_SECRET{sfx}"


def read_env(path: str | None = None) -> dict:
    """อ่าน .env สด ๆ ทุกครั้ง (key ที่เพิ่งเพิ่มใช้ได้ทันที) · ต้อง chmod 600"""
    path = path or ENV_PATH
    if not os.path.exists(path):
        return {}
    if os.stat(path).st_mode & 0o077:
        raise PermissionError(f"{path} ต้องอ่านได้เฉพาะเจ้าของ: chmod 600 {path}")
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def accounts(env: dict | None = None) -> list[dict]:
    """บัญชีทั้งหมดใน .env (ไม่ส่ง secret ออกไป) · บัญชีหลัก (ไม่มี suffix) มาก่อน"""
    env = read_env() if env is None else env
    out = []
    for k, v in env.items():
        m = ACCT_RE.match(k)
        if not m:
            continue
        acct = m.group(1) or ""
        _, sec = acct_env(acct)
        sfx = f"_{acct}" if acct else ""
        out.append({"acct": acct, "name": env.get(f"DERIBIT_NAME{sfx}") or "", "ready": bool(v and env.get(sec)),
                    "id_hint": (v[:4] + "…") if v else "", "testnet": env.get(f"DERIBIT_TESTNET{sfx}", "").lower() == "true"})
    return sorted(out, key=lambda a: (a["acct"] != "", a["acct"]))


def acct_code(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (name or "").upper()).strip("_")[:24]


async def _verify(testnet: bool, cid: str, secret: str) -> dict:
    c = DeribitClient(testnet, cid, secret)
    await c.connect()                                   # key ผิด → public/auth error
    try:
        a = await c.rpc("private/get_account_summary", {"currency": "BTC", "extended": True})
        return {"user": a.get("username") or a.get("system_name"), "type": a.get("type"), "equity": a.get("equity")}
    finally:
        await c.close()


def add_key(name: str, cid: str, secret: str, testnet: bool = False, verify=None) -> dict:
    """เพิ่ม API key ของ subaccount ลง .env — ทดสอบ login กับ Deribit ก่อน ไม่ผ่าน = ไม่บันทึก"""
    code = acct_code(name)
    cid, secret = (cid or "").strip(), (secret or "").strip()
    if not code:
        raise ValueError("ตั้งชื่อบัญชี (a-z 0-9) เช่น Beta3")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,100}", cid) or not re.fullmatch(r"[A-Za-z0-9_\-]{10,200}", secret):
        raise ValueError("client id / secret มีรูปแบบไม่ถูกต้อง (คัดลอกมาไม่ครบหรือมีช่องว่าง?)")
    env = read_env()
    if any(a["acct"] == code for a in accounts(env)):
        raise ValueError(f"มีบัญชีชื่อ {code} อยู่แล้ว")
    if any(ACCT_RE.match(k) and v == cid for k, v in env.items()):
        raise ValueError("key นี้มีอยู่แล้วใน .env (บัญชีอื่น)")
    try:
        info = asyncio.run((verify or _verify)(testnet, cid, secret))
    except Exception as e:
        raise ValueError(f"login Deribit ไม่ผ่าน — ไม่ได้บันทึก ({e})")
    lines = [f"# {name.strip()} — subaccount {info.get('user') or '?'} (เพิ่มจากหน้าเว็บ {time.strftime('%Y-%m-%d %H:%M')})",
             f"DERIBIT_NAME_{code}={name.strip()[:40]}", f"DERIBIT_CLIENT_ID_{code}={cid}", f"DERIBIT_CLIENT_SECRET_{code}={secret}"]
    if testnet:
        lines.append(f"DERIBIT_TESTNET_{code}=true")
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(("\n" if os.path.getsize(ENV_PATH) else "") + "\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    _acct_cache.clear()
    return {"ok": True, "acct": code, "user": info.get("user"), "type": info.get("type")}


def env_update(set_: dict | None = None, remove=()) -> str:
    """แก้ .env แบบคงบรรทัดอื่น/คอมเมนต์ไว้ · สำรองไฟล์เดิมเป็น .env.bak-<เวลา> (chmod 600) ก่อน · เขียนแบบ atomic + chmod 600"""
    set_ = dict(set_ or {})
    for k, v in set_.items():
        if not re.fullmatch(r"[A-Z0-9_]+", k) or re.search(r"[\s'\"#]", str(v)):
            raise ValueError(f"ค่าของ {k} มีอักขระที่ใช้ไม่ได้")
    lines = []
    if os.path.exists(ENV_PATH):
        read_env()                                             # ตรวจสิทธิ์ไฟล์ก่อน
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
        bak = f"{ENV_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    out, done = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in remove:
            continue
        if k in set_:
            out.append(f"{k}={set_[k]}")
            done.add(k)
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in set_.items() if k not in done]
    tmp = ENV_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)
    _acct_cache.clear()
    return ENV_PATH


def remove_key(acct: str) -> dict:
    """ลบ key ของบัญชีออกจาก .env (สำรองไฟล์เดิมไว้) · ห้ามลบถ้ามีบอทของบัญชีนี้กำลังรัน"""
    if acct not in {a["acct"] for a in accounts()}:
        raise ValueError("ไม่มีบัญชีนี้ใน .env")
    running = [z["file"] for z in list_zones() + list_fv() + list_kzm() if z.get("acct", "") == acct and z.get("state", {}).get("running")]
    if running:
        raise PermissionError(f"บอทของบัญชีนี้กำลังรัน ({', '.join(running)}) — หยุดก่อนลบ key")
    sfx = f"_{acct}" if acct else ""
    env_update(remove={f"DERIBIT_CLIENT_ID{sfx}", f"DERIBIT_CLIENT_SECRET{sfx}", f"DERIBIT_NAME{sfx}", f"DERIBIT_TESTNET{sfx}"})
    used = [z["file"] for z in list_zones() + list_fv() + list_kzm() if z.get("acct", "") == acct]
    return {"ok": True, "files_left": used}


def _tg(token: str, method: str, params: dict | None = None, timeout=10):
    import urllib.request, urllib.parse
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            j = json.load(r)
    except Exception as e:
        msg = str(e).replace(token, "<token>")                 # ห้ามให้ token หลุดไปในข้อความ error
        raise ValueError(f"Telegram ตอบ error: {msg}")
    if not j.get("ok"):
        raise ValueError(f"Telegram ตอบ error: {j.get('description')}")
    return j["result"]


_tg_cache: dict = {}


def tg_status() -> dict:
    """สถานะ Telegram จาก .env (ไม่ส่ง token ออกไป)"""
    env = read_env()
    tok, chat = env.get("TG_BOT_TOKEN") or "", env.get("TG_CHAT_ID") or ""
    out = {"has_token": bool(tok), "has_chat": bool(chat), "chat_hint": ("…" + chat[-4:]) if chat else ""}
    if tok:
        hit = _tg_cache.get(tok)
        if hit and time.time() - hit[0] < 120:
            out.update(hit[1])
        else:
            try:
                me = _tg(tok, "getMe")
                info = {"bot": "@" + (me.get("username") or "?"), "bot_name": me.get("first_name"), "token_ok": True}
            except ValueError as e:
                info = {"token_ok": False, "error": str(e)}
            _tg_cache[tok] = (time.time(), info)
            out.update(info)
    return out


def tg_set_token(token: str) -> dict:
    token = (token or "").strip()
    if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,60}", token):
        raise ValueError("รูปแบบ token ไม่ถูกต้อง (ได้จาก @BotFather เช่น 123456789:AA...)")
    me = _tg(token, "getMe")
    env_update({"TG_BOT_TOKEN": token})
    _tg_cache.clear()
    return {"ok": True, "bot": "@" + (me.get("username") or "?")}


def tg_find_chats() -> list[dict]:
    tok = read_env().get("TG_BOT_TOKEN")
    if not tok:
        raise ValueError("ยังไม่มี token — ใส่ token ก่อน")
    chats = {}
    for u in _tg(tok, "getUpdates", {"timeout": 0}):
        c = (u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat")
        if c:
            chats[str(c["id"])] = c.get("username") or c.get("title") or c.get("first_name") or ""
    return [{"id": k, "name": v} for k, v in chats.items()]


def tg_set_chat(chat_id: str) -> dict:
    chat_id = str(chat_id or "").strip()
    if not re.fullmatch(r"-?\d{3,20}", chat_id):
        raise ValueError("chat id ต้องเป็นตัวเลข")
    env_update({"TG_CHAT_ID": chat_id})
    return {"ok": True}


def tg_test() -> dict:
    env = read_env()
    if not (env.get("TG_BOT_TOKEN") and env.get("TG_CHAT_ID")):
        raise ValueError("ต้องมีทั้ง token และ chat id")
    _tg(env["TG_BOT_TOKEN"], "sendMessage", {"chat_id": env["TG_CHAT_ID"], "text": "✅ ทดสอบจากหน้าเว็บ RBSI Control — Telegram ใช้งานได้"})
    return {"ok": True}


def creds(acct: str, testnet: bool):
    env = read_env()
    id_env, sec_env = acct_env(acct)
    return testnet, env.get(id_env), env.get(sec_env)


async def _account(testnet, cid, secret, instrument):
    c = DeribitClient(testnet, cid, secret)
    await c.connect()
    try:
        if cid:                                         # ยิงพร้อมกัน (ทีละคำขอ ~0.2 วิ/รอบ ไป-กลับ Deribit)
            tk, acc, pos, oo = await asyncio.gather(c.ticker(instrument), c.rpc("private/get_account_summary", {"currency": "BTC", "extended": True}),
                                                    c.position(instrument), c.open_orders(instrument))
        else:
            tk = await c.ticker(instrument)
        out = {"mark": tk["mark_price"], "bid": tk.get("best_bid_price"), "ask": tk.get("best_ask_price"), "index": tk.get("index_price")}
        if cid:
            out.update(
                user=acc.get("username") or acc.get("system_name"), acc_type=acc.get("type"),
                equity=acc.get("equity"), available=acc.get("available_funds"), balance=acc.get("balance"),
                withdrawable=acc.get("available_withdrawal_funds"), pos_im=pos.get("initial_margin"),
                acc_mm=acc.get("maintenance_margin"), acc_im=acc.get("initial_margin"),
                position=pos.get("size") or 0, avg=pos.get("average_price"), pos_mm=pos.get("maintenance_margin"),
                pos_liq=pos.get("estimated_liquidation_price"), upnl=pos.get("floating_profit_loss"),
                orders=[{"id": o["order_id"], "side": o["direction"], "price": o.get("price"), "amount": o["amount"],
                         "filled": o.get("filled_amount") or 0, "label": o.get("label") or "", "reduce_only": bool(o.get("reduce_only"))}
                        for o in oo])
        return out
    finally:
        await c.close()


def account(acct: str, testnet: bool, instrument="BTC-PERPETUAL") -> dict:
    tn, cid, secret = creds(acct, testnet)
    key = (tn, cid, instrument)
    hit = _acct_cache.get(key)
    if hit and time.time() - hit[0] < 4:
        return hit[1]
    out = asyncio.run(_account(tn, cid, secret, instrument))
    out.update(has_key=bool(cid and secret), testnet=tn, ts=time.time(), acct=acct)
    _acct_cache[key] = (time.time(), out)
    return out


# ---------- สั่งบอท ----------
def cli(args: list[str], timeout=90) -> dict:
    try:
        r = subprocess.run([sys.executable, "-m", "rbsi_bot", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "code": r.returncode, "out": (r.stdout + r.stderr).strip()}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "code": None, "out": f"หมดเวลา {timeout} วิ\n{e.stdout or ''}"}


_spawned: set[int] = set()        # PID ที่ process หน้าเว็บนี้เริ่มเอง (มี thread รอเก็บอยู่แล้ว)


def reap_zombies() -> list[int]:
    """เก็บ process ลูกที่จบแล้วแต่ค้างเป็น zombie — บอทที่เริ่มก่อนหน้าเว็บรีสตาร์ทตัวเอง (os.execv) ไม่มี thread รอเก็บแล้ว
    พอหยุด/บอทจบ จึงค้างใต้หน้าเว็บ · ใช้ ps (pgrep มองไม่เห็น zombie) · ข้ามตัวที่ thread ของ spawn() เก็บอยู่"""
    r = subprocess.run(["ps", "-axo", "pid=,ppid=,stat="], capture_output=True, text=True)
    me, out = os.getpid(), []
    for line in r.stdout.splitlines():
        x = line.split()
        if len(x) >= 3 and x[1] == str(me) and x[2].startswith("Z") and int(x[0]) not in _spawned:
            try:
                if os.waitpid(int(x[0]), os.WNOHANG)[0]:
                    out.append(int(x[0]))
            except ChildProcessError:
                pass
    return out


def spawn(rel_cfg: str, state_dir: str, lock_id: str, extra: list[str], wait=10.0, live=True) -> dict:
    """เริ่มบอท LIVE เป็น process แยก (ปิดหน้าเว็บ/เซิร์ฟเวอร์แล้วบอทยังรันต่อ) · ใช้ caffeinate -i กันเครื่องหลับ"""
    lp = lock_path(state_dir, lock_id)
    if is_locked(lp):
        return {"ok": False, "out": f"บอทนี้รันอยู่แล้ว (PID {lock_pids(lp)})"}
    d = os.path.expanduser(state_dir)
    os.makedirs(d, exist_ok=True)
    outp = os.path.join(d, f"{lock_id}.out")
    cmd = ["caffeinate", "-i", sys.executable, "-m", "rbsi_bot", "run", "-c", rel_cfg, *(["--live"] if live else []), *extra]
    pos0 = os.path.getsize(outp) if os.path.exists(outp) else 0
    with open(outp, "ab") as fo:
        fo.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} เริ่มจากหน้าเว็บ: {' '.join(cmd[3:])}\n".encode())
        fo.flush()
        p = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.STDOUT, start_new_session=True)
    _spawned.add(p.pid)
    threading.Thread(target=p.wait, daemon=True).start()          # เก็บ process ที่จบแล้ว (ไม่ให้ค้างเป็น zombie)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(0.2)
        if p.poll() is not None:
            break
        if is_locked(lp):                                            # ถือ lock แล้ว + ผ่านด่านตอนเริ่ม (ข้อความ ▶️ เริ่มบอท) หรือรอครบ 3 วิ
            with open(outp, "rb") as fo:
                fo.seek(pos0)
                if "▶️ เริ่มบอท".encode() in fo.read() or time.time() - t0 > 3:
                    break
    with open(outp, encoding="utf-8", errors="replace") as fo:
        fo.seek(pos0)
        tail = fo.read()[-4000:]
    alive = p.poll() is None
    return {"ok": alive, "pid": p.pid, "out": tail.strip() + ("" if alive else f"\n\n(process จบแล้ว code {p.returncode})")}


def stop_lock(state_dir: str, lock_id: str, wait=20.0, note="") -> dict:
    """ส่ง SIGINT (เหมือนกด Ctrl+C) ให้ process ที่ถือ lock นี้ — ออเดอร์ที่วางไว้ยังค้างบน Deribit ตามปกติ"""
    lp = lock_path(state_dir, lock_id)
    pids = lock_pids(lp)
    if not pids:
        return {"ok": True, "out": "ไม่มีบอทนี้รันอยู่"}
    for pid in pids:
        os.kill(pid, signal.SIGINT)
    t0 = time.time()
    while time.time() - t0 < wait and is_locked(lp):
        time.sleep(0.2)
    if is_locked(lp):
        for pid in lock_pids(lp):
            os.kill(pid, signal.SIGTERM)
        time.sleep(1.5)
    ok = not is_locked(lp)
    outp = os.path.join(os.path.expanduser(state_dir), f"{lock_id}.out")
    if ok and os.path.exists(outp):                                 # บันทึกว่าหยุดเอง (ไม่ใช่บอทตาย)
        with open(outp, "a", encoding="utf-8") as fo:
            fo.write(f"\n===== หยุดจากหน้าเว็บ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    return {"ok": ok, "out": f"หยุด PID {pids} แล้ว{note}" if ok else f"หยุด PID {pids} ไม่สำเร็จ"}


def tail(state_dir: str, lock_id: str, n=150) -> str:
    p = os.path.join(os.path.expanduser(state_dir), lock_id + ".log")
    if not os.path.exists(p):
        return ""
    with open(p, "rb") as fo:
        fo.seek(max(0, os.path.getsize(p) - 60_000))
        lines = fo.read().decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def start_bot(file: str, adopt_manual: bool, clear_halt: bool, wait=10.0, dry=False, force=False) -> dict:
    """dry = รันโดยไม่ส่งคำสั่งจริง (ไม่ต้อง live_allowed) · force = ข้ามด่าน liq ตอนเริ่ม (live เท่านั้น)"""
    f = read_zone(file)
    if not dry and not f["live"]:
        return {"ok": False, "out": "ไฟล์นี้ยังไม่อนุญาต live (live_allowed = false) — แก้ในฟอร์มแล้วบันทึกก่อน หรือเริ่มแบบ dry-run"}
    return spawn(os.path.relpath(zone_path(file), ROOT), f["state_dir"], zone_cfg(f).zone_id,
                 ["--adopt-manual"] * adopt_manual + ["--clear-halt"] * clear_halt + ["--force"] * (force and not dry), wait, live=not dry)


def stop_bot(file: str, wait=20.0) -> dict:
    f = read_zone(file)
    return stop_lock(f["state_dir"], zone_cfg(f).zone_id, wait,
                     " — ออเดอร์ของบอทยังค้างบน Deribit (fill ได้ตามปกติ แต่จะไม่มีออเดอร์ฝั่งตรงข้ามจนกว่าจะเริ่มบอทใหม่)")


def tail_log(file: str, n=150) -> str:
    f = read_zone(file)
    return tail(f["state_dir"], zone_cfg(f).zone_id, n)


def delete_zone(file: str) -> dict:
    """ลบไฟล์ zone = ย้ายไป zones/.bak/<ชื่อ>.deleted-<เวลา>.toml (กู้คืนได้) · ไม่แตะสถานะใน ~/.rbsi-bot และไม่แตะออเดอร์บน Deribit
    ห้ามลบไฟล์ที่บอทกำลังรัน"""
    path = zone_path(file)
    if not os.path.exists(path):
        raise FileNotFoundError(f"ไม่มีไฟล์ zones/{file}.toml")
    try:
        st = zone_state(read_zone(file))
    except Exception:                                   # ไฟล์เสีย/อ่านไม่ได้ ก็ลบได้
        st = {"running": False, "status": None, "zone_id": None}
    if st["running"]:
        raise PermissionError("บอทของไฟล์นี้กำลังรัน — หยุดบอทก่อนลบ")
    bak = os.path.join(ZONES, ".bak")
    os.makedirs(bak, exist_ok=True)
    dst = os.path.join(bak, f"{file}.deleted-{time.strftime('%Y%m%d-%H%M%S')}.toml")
    os.replace(path, dst)
    return {"ok": True, "moved_to": os.path.relpath(dst, ROOT), "status": st["status"], "zone_id": st["zone_id"]}


# ---------- บอท Fixed Value (ไฟล์ใน fv/) ----------
FV_DIR = os.path.join(ROOT, "fv")
FV_SAFETY_DEF = {"settle_sec": 0.3, "debounce_sec": 0.1, "reconcile_sec": 30, "pending_ttl": 60, "flux_halt_sec": 180,
                 "cash_accept_sec": 10, "heartbeat_sec": 300, "summary_hours": 12, "order_spacing_sec": 0.15, "throttle_max_sec": 30}
FV_DEF = {"instrument": "BTC_USDC", "mode": "usd", "min_order_usd": 5, "max_order_usd": 0, "fee_rate": 0.0002, "chase_bps": 5}


def fv_path(file: str) -> str:
    if not FILE_RE.match(file or "") or file.startswith("."):
        raise ValueError("ชื่อไฟล์ใช้ได้แค่ a-z 0-9 . _ -")
    return os.path.join(FV_DIR, file + ".toml")


def read_fv(file: str) -> dict:
    with open(fv_path(file), "rb") as f:
        c = tomllib.load(f)
    v, d = {**FV_DEF, **c["fixed_value"]}, c.get("deribit", {})
    m = ACCT_RE.match(d.get("client_id_env", "DERIBIT_CLIENT_ID"))
    return {"file": file, "name": c.get("name", ""), "acct": (m.group(1) or "") if m else "?" + d.get("client_id_env", ""),
            "live": bool(c.get("live_allowed", False)), "testnet": bool(d.get("testnet", False)), "state_dir": c.get("state_dir", "~/.rbsi-bot"),
            **{k: v[k] for k in ("instrument", "value", "mode", "step", "min_order_usd", "max_order_usd", "fee_rate", "chase_bps")},
            **{**FV_SAFETY_DEF, **{k: x for k, x in c.get("safety", {}).items() if k in FV_SAFETY_DEF}}}


def fv_cfg(f: dict):
    from .fv import FVConfig
    return FVConfig(value=float(f["value"]), mode=str(f.get("mode") or "usd"), step=float(f["step"]), instrument=str(f.get("instrument") or "BTC_USDC"),
                    min_order_usd=float(f.get("min_order_usd", 5)), max_order_usd=float(f.get("max_order_usd") or 0),
                    fee_rate=float(f.get("fee_rate", 0.0002)), chase_bps=float(f.get("chase_bps", 5)))


def fv_toml(f: dict) -> str:
    """TOML ของบอท Fixed Value — ตรวจด้วย FVConfig ตัวเดียวกับบอท"""
    f = {**FV_DEF, **FV_SAFETY_DEF, **{k: v for k, v in f.items() if v is not None and v != ""}}
    for k in ("value", "step", "min_order_usd", "max_order_usd", "fee_rate", "chase_bps", *FV_SAFETY_DEF):
        if not math.isfinite(float(f[k])) or float(f[k]) < 0:
            raise ValueError(f"{k} ต้องเป็นตัวเลขไม่ติดลบ")
    cfg = fv_cfg(f)
    if not 0 <= cfg.fee_rate < 0.01 or not 0 < cfg.chase_bps < 500:
        raise ValueError("fee_rate / chase_bps ไม่สมเหตุสมผล")
    acct = str(f.get("acct") or "")
    if acct and not re.fullmatch(r"[A-Z0-9_]+", acct):
        raise ValueError("รหัสบัญชีไม่ถูกต้อง")
    id_env, secret_env = acct_env(acct)
    name = re.sub(r'["\\\n\r]', "", str(f.get("name") or ""))[:40]
    b = lambda x: "true" if x in (True, "true", 1) else "false"
    n = lambda k: fmtnum(float(f[k]))
    return f'''# บอท Fixed Asset Value — สร้างจากหน้าเว็บ (rbsi_bot ui) · {time.strftime("%Y-%m-%d %H:%M")}
# ใช้ subaccount แยกเฉพาะบอทนี้: บอทถือว่า {cfg.base} และ {cfg.quote} ทั้งหมดใน sub เป็นของมัน
name = "{name}"
env_file = "../.env"
state_dir = "{f.get("state_dir") or "~/.rbsi-bot"}"
live_allowed = {b(f.get("live"))}

[deribit]
testnet = {b(f.get("testnet"))}
client_id_env = "{id_env}"
client_secret_env = "{secret_env}"

[fixed_value]
instrument = "{cfg.instrument}"
value = {fmtnum(cfg.value)}                         # มูลค่า {cfg.base} ที่ถือคงไว้ (USD)
mode = "{cfg.mode}"                          # "usd" = step เป็น USD · "pct" = step เป็น % ของ value
step = {fmtnum(cfg.step)}                          # Δ = ${cfg.delta:,.2f}
min_order_usd = {fmtnum(cfg.min_order_usd)}
max_order_usd = {fmtnum(cfg.max_order_usd)}                   # 0 = ไม่จำกัด
fee_rate = {fmtnum(cfg.fee_rate)}
chase_bps = {fmtnum(cfg.chase_bps)}

[safety]
{chr(10).join(f"{k} = {n(k)}" for k in FV_SAFETY_DEF)}

[telegram]
token_env = "TG_BOT_TOKEN"
chat_id_env = "TG_CHAT_ID"
'''


def fv_state(f: dict) -> dict:
    from .fvbot import FVStore
    cfg = fv_cfg(f)
    st = FVStore(f["state_dir"], cfg.fv_id).data
    lp = lock_path(f["state_dir"], cfg.fv_id)
    running = is_locked(lp)
    pids = lock_pids(lp) if running else []                  # lsof ช้า (~0.2 วิ) → เรียกครั้งเดียว
    return {"fv_id": cfg.fv_id, "delta": cfg.delta, "status": st.get("status"), "halt_reason": st.get("halt_reason", ""),
            "running": running, "pids": pids, "heartbeat": st.get("heartbeat") or {},
            "fills": (st.get("fills") or [])[-10:], "n_fills": len(st.get("fills") or []), "exp": st.get("exp"), "start": st.get("start"),
            "fees_usd": st.get("fees_usd", 0.0), "saved": os.path.exists(fv_path(f["file"])),
            "mode": pid_mode(pids) if running else None, "crash": None if running else crash_info(f["state_dir"], cfg.fv_id)}


def list_fv() -> list[dict]:
    out = []
    for fn in sorted(os.listdir(FV_DIR)) if os.path.isdir(FV_DIR) else []:
        if not fn.endswith(".toml") or fn.startswith("example"):
            continue
        try:
            f = read_fv(fn[:-5])
            out.append({**f, "state": fv_state(f)})
        except Exception as e:
            out.append({"file": fn[:-5], "error": str(e)})
    return out


def fv_preview(fields: dict, bid=None, ask=None, btc=None, usdc=None, band=None) -> dict:
    """TOML + แผนจากค่าในฟอร์ม ณ ยอด/ราคาจริง (ใช้ตรรกะเดียวกับบอท)"""
    from .fv import desired, ladder, cash_runs_out, triggers
    text = fv_toml(fields)
    cfg = fv_cfg({**FV_DEF, **fields})
    out = {"toml": text, "fv_id": cfg.fv_id, "delta": cfg.delta, "round_trip": cfg.delta ** 2 / cfg.value - 2 * cfg.delta * cfg.fee_rate}
    if bid and ask:
        mid = (float(bid) + float(ask)) / 2
        qty = float(btc) if btc is not None else cfg.value / mid
        cash = float(usdc or 0)
        dev = qty * mid - cfg.value
        # ห่างเป้า ≥ Δ → ตอนเริ่มบอทจะปรับเข้าเป้าก่อน: ราคาขาย/ซื้อถัดไป บันได และจุด USDC หมด คิดจากยอดหลังปรับ
        after = abs(dev) >= cfg.delta
        qa = cfg.value / mid if after else qty
        ca = (cash - (qa - qty) * mid * (1 + cfg.fee_rate) if qa > qty else cash + (qty - qa) * mid * (1 - cfg.fee_rate)) if after else cash
        ps, pb = triggers(cfg, qa)
        out.update(mid=mid, qty=qty, cash=cash, value=qty * mid, dev=dev, after=after, cash_after=ca, sell_at=ps, buy_at=None if pb == math.inf else pb,
                   sell=desired(cfg, "sell", qty, float(bid), float(ask)), buy=desired(cfg, "buy", qty, float(bid), float(ask)),
                   ladder=ladder(cfg, qa, max(ca, 0.0), 5), cash_out=cash_runs_out(cfg, qa, max(ca, 0.0)), cash_short=ca < 0)
        if band and band[0] and band[1]:                              # กรอบราคาของ Deribit: ออเดอร์นอกกรอบยังไม่วาง
            lo, hi = float(band[0]), float(band[1])
            out["band"] = [lo, hi]
            out["out_of_band"] = {s_: bool(out[s_]) and not (lo <= out[s_][0] <= hi) for s_ in ("sell", "buy")}
    return out


async def _fv_account(testnet, cid, secret, instrument):
    base, quote = instrument.split("_")
    c = DeribitClient(testnet, cid, secret)
    await c.connect()
    try:
        if cid:                                         # ยิงพร้อมกัน (ทีละคำขอ ~0.2 วิ/รอบ ไป-กลับ Deribit)
            tk, a, q, oo, perp = await asyncio.gather(c.ticker(instrument), c.rpc("private/get_account_summary", {"currency": base, "extended": True}),
                                                      c.account_summary(quote), c.open_orders(instrument), c.position(f"{base}-PERPETUAL"))
        else:
            tk = await c.ticker(instrument)
        out = {"bid": tk.get("best_bid_price"), "ask": tk.get("best_ask_price"), "mark": tk.get("mark_price"), "last": tk.get("last_price"),
               "min_price": tk.get("min_price"), "max_price": tk.get("max_price")}
        if cid:
            out.update(user=a.get("username") or a.get("system_name"), acct_type=a.get("type"), btc=a.get("balance"), usdc=q.get("balance"),
                       btc_reserved=a.get("spot_reserve"), usdc_reserved=q.get("spot_reserve"), perp=perp.get("size") or 0,
                       orders=[{"id": o["order_id"], "side": o["direction"], "price": o.get("price"), "amount": o["amount"],
                                "filled": o.get("filled_amount") or 0, "label": o.get("label") or ""} for o in oo])
        return out
    finally:
        await c.close()


def fv_account(acct: str, testnet: bool, instrument="BTC_USDC") -> dict:
    tn, cid, secret = creds(acct, testnet)
    key = ("fv", tn, cid, instrument)
    hit = _acct_cache.get(key)
    if hit and time.time() - hit[0] < 4:
        return hit[1]
    out = asyncio.run(_fv_account(tn, cid, secret, instrument))
    out.update(has_key=bool(cid and secret), testnet=tn, ts=time.time(), acct=acct)
    _acct_cache[key] = (time.time(), out)
    return out


def fv_save(fields: dict, overwrite: bool) -> dict:
    if (fields.get("acct") or "") not in {a["acct"] for a in accounts()}:
        raise ValueError(f"ไม่มี key ของบัญชี {fields.get('acct') or 'หลัก'} ใน .env")
    path = fv_path(fields["file"])
    text = fv_toml(fields)
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(f"มีไฟล์ fv/{fields['file']}.toml อยู่แล้ว")
        if fv_state(read_fv(fields["file"]))["running"]:
            raise PermissionError("บอทของไฟล์นี้กำลังรัน — หยุดบอทก่อนแก้ไฟล์")
        bak = os.path.join(FV_DIR, ".bak")
        os.makedirs(bak, exist_ok=True)
        os.replace(path, os.path.join(bak, f"{fields['file']}.{time.strftime('%Y%m%d-%H%M%S')}.toml"))
    os.makedirs(FV_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fo:
        fo.write(text)
    return {"ok": True, "file": fields["file"]}


def fv_delete(file: str) -> dict:
    path = fv_path(file)
    if not os.path.exists(path):
        raise FileNotFoundError(f"ไม่มีไฟล์ fv/{file}.toml")
    try:
        running = fv_state(read_fv(file))["running"]
    except Exception:
        running = False
    if running:
        raise PermissionError("บอทของไฟล์นี้กำลังรัน — หยุดบอทก่อนลบ")
    bak = os.path.join(FV_DIR, ".bak")
    os.makedirs(bak, exist_ok=True)
    dst = os.path.join(bak, f"{file}.deleted-{time.strftime('%Y%m%d-%H%M%S')}.toml")
    os.replace(path, dst)
    return {"ok": True, "moved_to": os.path.relpath(dst, ROOT)}


def fv_flags(body: dict) -> list[str]:
    return (["--rebalance-now"] * bool(body.get("rebalance_now")) + ["--accept-balance"] * bool(body.get("accept_balance"))
            + ["--clear-halt"] * bool(body.get("clear_halt")))


# ---------- บอท KZM Grid (ไฟล์ใน kzm/) — ใช้ key/บัญชี/ปุ่มควบคุมแบบเดียวกับ Fixed Value ----------
KZM_DIR = os.path.join(ROOT, "kzm")
KZM_DEF = {"instrument": "BTC_USDC", "min_order_usd": 5, "fee_rate": 0.0002, "chase_bps": 5, "max_orders": 5, "max_chase": 3}
KZM_NUM = ("floor", "ceil", "gap", "capital", "min_order_usd", "fee_rate", "chase_bps", "max_orders", "max_chase")


def kzm_path(file: str) -> str:
    if not FILE_RE.match(file or "") or file.startswith("."):
        raise ValueError("ชื่อไฟล์ใช้ได้แค่ a-z 0-9 . _ -")
    return os.path.join(KZM_DIR, file + ".toml")


def read_kzm(file: str) -> dict:
    with open(kzm_path(file), "rb") as f:
        c = tomllib.load(f)
    v, d = {**KZM_DEF, **c["kzm"]}, c.get("deribit", {})
    m = ACCT_RE.match(d.get("client_id_env", "DERIBIT_CLIENT_ID"))
    return {"file": file, "name": c.get("name", ""), "acct": (m.group(1) or "") if m else "?" + d.get("client_id_env", ""),
            "live": bool(c.get("live_allowed", False)), "testnet": bool(d.get("testnet", False)), "state_dir": c.get("state_dir", "~/.rbsi-bot"),
            "instrument": v["instrument"], **{k: v[k] for k in KZM_NUM},
            **{**FV_SAFETY_DEF, **{k: x for k, x in c.get("safety", {}).items() if k in FV_SAFETY_DEF}}}


def kzm_cfg(f: dict):
    from .kzm import KZMConfig
    return KZMConfig(floor=float(f["floor"]), ceil=float(f["ceil"]), gap=float(f["gap"]), capital=float(f["capital"]),
                     instrument=str(f.get("instrument") or "BTC_USDC"), min_order_usd=float(f.get("min_order_usd", 5)),
                     fee_rate=float(f.get("fee_rate", 0.0002)), chase_bps=float(f.get("chase_bps", 5)),
                     max_orders=int(float(f.get("max_orders", 5))), max_chase=int(float(f.get("max_chase", 3))))


def kzm_toml(f: dict) -> str:
    """TOML ของบอท KZM — ตรวจด้วย KZMConfig ตัวเดียวกับบอท"""
    f = {**KZM_DEF, **FV_SAFETY_DEF, **{k: v for k, v in f.items() if v is not None and v != ""}}
    for k in (*KZM_NUM, *FV_SAFETY_DEF):
        if not math.isfinite(float(f[k])) or float(f[k]) < 0:
            raise ValueError(f"{k} ต้องเป็นตัวเลขไม่ติดลบ")
    cfg = kzm_cfg(f)
    if not 0 <= cfg.fee_rate < 0.01 or not 0 < cfg.chase_bps < 500 or cfg.max_orders > 20 or cfg.max_chase > 10:
        raise ValueError("fee_rate / chase_bps / max_orders (≤ 20) / max_chase (≤ 10) ไม่สมเหตุสมผล")
    acct = str(f.get("acct") or "")
    if acct and not re.fullmatch(r"[A-Z0-9_]+", acct):
        raise ValueError("รหัสบัญชีไม่ถูกต้อง")
    id_env, secret_env = acct_env(acct)
    name = re.sub(r'["\\\n\r]', "", str(f.get("name") or ""))[:40]
    b = lambda x: "true" if x in (True, "true", 1) else "false"
    n = lambda k: fmtnum(float(f[k]))
    return f'''# บอท KZM Grid (Dynamic Ratio) — สร้างจากหน้าเว็บ (rbsi_bot ui) · {time.strftime("%Y-%m-%d %H:%M")}
# ใช้ subaccount แยกเฉพาะบอทนี้: บอทถือว่า {cfg.base} และ {cfg.quote} ทั้งหมดใน sub เป็นของกริด
name = "{name}"
env_file = "../.env"
state_dir = "{f.get("state_dir") or "~/.rbsi-bot"}"
live_allowed = {b(f.get("live"))}

[deribit]
testnet = {b(f.get("testnet"))}
client_id_env = "{id_env}"
client_secret_env = "{secret_env}"

[kzm]
instrument = "{cfg.instrument}"
floor = {fmtnum(cfg.floor)}                     # ชั้นแรกซื้อที่ราคานี้ (ต่ำกว่านี้ = ถือ {cfg.base} เต็มกริด)
ceil = {fmtnum(cfg.ceil)}                      # ชั้นสุดท้ายขายที่ราคานี้ (สูงกว่านี้ = เงินสด 100%)
gap = {fmtnum(cfg.gap)}                        # {cfg.n} ชั้น
capital = {fmtnum(cfg.capital)}                   # ทุนรวม (USD) → ชั้นละ ${cfg.cost:,.2f}
min_order_usd = {fmtnum(cfg.min_order_usd)}
fee_rate = {fmtnum(cfg.fee_rate)}
chase_bps = {fmtnum(cfg.chase_bps)}
max_orders = {cfg.max_orders}                     # ออเดอร์รอที่ราคาชั้น ฝั่งละ (ชั้นใกล้ราคาที่สุด)
max_chase = {cfg.max_chase}                      # ออเดอร์ไล่ราคา (ชั้นที่ราคาทะลุแล้ว) ฝั่งละต่อครั้ง

[safety]
{chr(10).join(f"{k} = {n(k)}" for k in FV_SAFETY_DEF)}

[telegram]
token_env = "TG_BOT_TOKEN"
chat_id_env = "TG_CHAT_ID"
'''


def kzm_state(f: dict) -> dict:
    from .kzmbot import KZMStore
    cfg = kzm_cfg(f)
    st = KZMStore(f["state_dir"], cfg.fv_id).data
    lp = lock_path(f["state_dir"], cfg.fv_id)
    running = is_locked(lp)
    pids = lock_pids(lp) if running else []                  # lsof ช้า (~0.2 วิ) → เรียกครั้งเดียว
    return {"fv_id": cfg.fv_id, "n": cfg.n, "cost": cfg.cost, "status": st.get("status"), "halt_reason": st.get("halt_reason", ""),
            "running": running, "pids": pids, "heartbeat": st.get("heartbeat") or {},
            "fills": (st.get("fills") or [])[-10:], "n_fills": len(st.get("fills") or []), "exp": st.get("exp"), "start": st.get("start"),
            "fees_usd": st.get("fees_usd", 0.0), "grid_pnl": st.get("grid_pnl", 0.0), "init_pnl": st.get("init_pnl", 0.0), "rounds": st.get("rounds", 0),
            "saved": os.path.exists(kzm_path(f["file"])),
            "mode": pid_mode(pids) if running else None, "crash": None if running else crash_info(f["state_dir"], cfg.fv_id)}


def list_kzm() -> list[dict]:
    out = []
    for fn in sorted(os.listdir(KZM_DIR)) if os.path.isdir(KZM_DIR) else []:
        if not fn.endswith(".toml") or fn.startswith("example"):
            continue
        try:
            f = read_kzm(fn[:-5])
            out.append({**f, "state": kzm_state(f)})
        except Exception as e:
            out.append({"file": fn[:-5], "error": str(e)})
    return out


def kzm_preview(fields: dict, bid=None, ask=None, btc=None, usdc=None, band=None, orders=None) -> dict:
    """TOML + ภาพกริดจากค่าในฟอร์ม ณ ยอด/ราคาจริง (ใช้ตรรกะเดียวกับบอท)
    บอทเริ่มแล้ว (มีบัญชีไม้) → ใช้ชั้นจากไฟล์สถานะ · ยังไม่เริ่ม → สมมติเริ่มแบบ ratio ตอนนี้ (BTC ที่มีนับเข้าชั้นก่อน)"""
    from .kzm import start_cap, allocate, start_plan, cash_to_floor, cash_runs_out, plan, full, parse_label
    from .kzmbot import KZMStore
    from .grid import Order
    text = kzm_toml(fields)
    cfg = kzm_cfg({**KZM_DEF, **fields})
    out = {"toml": text, "fv_id": cfg.fv_id, "n": cfg.n, "cost": cfg.cost,
           "round_trip": cfg.cost / ((cfg.floor + cfg.ceil) / 2) * cfg.gap - 2 * cfg.cost * cfg.fee_rate}
    if not (bid and ask):
        return out
    bid, ask = float(bid), float(ask)
    mid = (bid + ask) / 2
    qty, cash = float(btc or 0), float(usdc or 0)
    st = KZMStore(fields.get("state_dir") or "~/.rbsi-bot", cfg.fv_id).data if fields.get("file") else {}
    started = st.get("status") in ("active", "halted") and st.get("start")
    if started:
        h = {int(k): float(v["h"]) for k, v in st["layers"].items() if float(v["h"]) > 0}
        cap = {int(k): float(v) for k, v in st.get("cap", {}).items()}
        cash = float(st["exp"]["quote"])
    else:
        cap = start_cap(cfg, mid)
        try:
            h = allocate(cfg, {}, qty, mid, cap)
        except ValueError as e:
            out.update(mid=mid, error_btc=str(e))
            return out
        sp = start_plan(cfg, h, ask, cap)
        out["start"] = sp
    own = []
    for o in orders or []:
        kk = parse_label(cfg, o.get("label"))
        if kk:
            own.append((kk, Order(o["id"], o["side"], float(o["price"]), float(o["amount"]), float(o.get("filled") or 0), o.get("label") or "")))
    b = (float(band[0]), float(band[1])) if band and band[0] and band[1] else None
    dec = plan(cfg, h, cash, bid, ask, own if started else [], band=b, cap=cap)
    if not started:                                  # ภาพหลังเริ่ม: ชั้นตั้งต้นถือครบ
        h_after = {**h, **{k: max(h.get(k, 0.0), v) for k, v in cap.items()}}
        cash_after = cash - out["start"]["buy_usd"] * (1 + cfg.fee_rate) + out["start"]["sell_usd"] * (1 - cfg.fee_rate)
    else:
        h_after, cash_after = h, cash
    held = sum(h_after.values())
    out["surplus"] = cash_after - cash_to_floor(cfg, h_after, cap, mid)
    out.update(mid=mid, started=bool(started), qty=qty, cash=cash, cash_after=cash_after, btc_after=held, value_after=held * mid,
               share_after=held * mid / (held * mid + cash_after) if held * mid + cash_after > 0 else 0,
               to_floor=cash_to_floor(cfg, h_after, cap, mid), cash_out=cash_runs_out(cfg, h_after, max(cash_after, 0.0), mid, cap),
               actions=[a for a in dec.actions if a[0] == "place"], info={k: v for k, v in dec.info.items() if k != "waiting_band"},
               waiting=dec.info["waiting_band"], band=list(b) if b else None)
    # ตารางชั้นรอบราคา: 8 ชั้นเหนือ/ใต้
    k0 = max(0, min(cfg.n - 1, int((mid - cfg.floor) // cfg.gap)))
    rows = []
    for k in range(min(cfg.n - 1, k0 + 8), max(-1, k0 - 9), -1):
        f_ = full(cfg, k, cap)
        rows.append({"k": k, "buy": cfg.buy_price(k), "sell": cfg.sell_price(k), "full": f_, "h": h_after.get(k, 0.0) if not started else h.get(k, 0.0)})
    out["layers"] = rows
    return out


def kzm_save(fields: dict, overwrite: bool) -> dict:
    if (fields.get("acct") or "") not in {a["acct"] for a in accounts()}:
        raise ValueError(f"ไม่มี key ของบัญชี {fields.get('acct') or 'หลัก'} ใน .env")
    path = kzm_path(fields["file"])
    text = kzm_toml(fields)
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(f"มีไฟล์ kzm/{fields['file']}.toml อยู่แล้ว")
        if kzm_state(read_kzm(fields["file"]))["running"]:
            raise PermissionError("บอทของไฟล์นี้กำลังรัน — หยุดบอทก่อนแก้ไฟล์")
        bak = os.path.join(KZM_DIR, ".bak")
        os.makedirs(bak, exist_ok=True)
        os.replace(path, os.path.join(bak, f"{fields['file']}.{time.strftime('%Y%m%d-%H%M%S')}.toml"))
    os.makedirs(KZM_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fo:
        fo.write(text)
    return {"ok": True, "file": fields["file"]}


def kzm_delete(file: str) -> dict:
    path = kzm_path(file)
    if not os.path.exists(path):
        raise FileNotFoundError(f"ไม่มีไฟล์ kzm/{file}.toml")
    try:
        running = kzm_state(read_kzm(file))["running"]
    except Exception:
        running = False
    if running:
        raise PermissionError("บอทของไฟล์นี้กำลังรัน — หยุดบอทก่อนลบ")
    bak = os.path.join(KZM_DIR, ".bak")
    os.makedirs(bak, exist_ok=True)
    dst = os.path.join(bak, f"{file}.deleted-{time.strftime('%Y%m%d-%H%M%S')}.toml")
    os.replace(path, dst)
    return {"ok": True, "moved_to": os.path.relpath(dst, ROOT)}


# ---------- HTTP ----------
PKG = os.path.dirname(os.path.abspath(__file__))
_active = 0                                           # request ที่กำลังทำงาน (รีสตาร์ทตัวเองเฉพาะตอนว่าง)
_active_lock = threading.Lock()


def code_ver(exts=(".py", ".html")) -> float:
    """เวลาแก้ไขล่าสุดของโค้ดบอท/หน้าเว็บ — เปลี่ยน = มีโค้ดใหม่"""
    return max(os.path.getmtime(os.path.join(PKG, f)) for f in os.listdir(PKG) if f.endswith(exts))


class Handler(BaseHTTPRequestHandler):
    token = ""
    port = 0

    def handle_one_request(self):
        global _active
        with _active_lock:
            _active += 1
        try:
            super().handle_one_request()
        finally:
            with _active_lock:
                _active -= 1

    def log_message(self, fmt, *args):          # เงียบ ไม่พิมพ์ทุก request
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _guard(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self._send(403, {"error": "host"})
            return False
        if self.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
            self._send(403, {"error": "token"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                with open(os.path.join(os.path.dirname(__file__), "ui.html"), encoding="utf-8") as fo:
                    html = fo.read().replace("__TOKEN__", self.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path == "/api/zones":
                return self._send(200, {"zones": list_zones(), "env": os.path.exists(os.path.join(ROOT, ".env")), "ver": code_ver()})
            if u.path == "/api/account":
                return self._send(200, account(q.get("acct", ""), q.get("testnet") == "true"))
            if u.path == "/api/transfer":
                f = read_zone(q["file"])
                a = account(f["acct"], f["testnet"], f["instrument"])
                return self._send(200, transferable(zone_cfg(f), a) if a.get("has_key") else {"error": "ไม่มี key"})
            if u.path == "/api/accounts":
                return self._send(200, {"accounts": accounts()})
            if u.path == "/api/tg/status":
                return self._send(200, tg_status())
            if u.path == "/api/log":
                return self._send(200, {"log": tail_log(q["file"])})
            if u.path == "/api/fv/list":
                return self._send(200, {"bots": list_fv(), "ver": code_ver()})
            if u.path == "/api/fv/account":
                return self._send(200, fv_account(q.get("acct", ""), q.get("testnet") == "true"))
            if u.path == "/api/kzm/list":
                return self._send(200, {"bots": list_kzm(), "ver": code_ver()})
            if u.path == "/api/kzm/log":
                f = read_kzm(q["file"])
                return self._send(200, {"log": tail(f["state_dir"], kzm_cfg(f).fv_id)})
            if u.path == "/api/fv/log":
                f = read_fv(q["file"])
                return self._send(200, {"log": tail(f["state_dir"], fv_cfg(f).fv_id)})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _fv_post(self, op: str, body: dict):
        if op == "preview":
            return self._send(200, fv_preview(body["fields"], body.get("bid"), body.get("ask"), body.get("btc"), body.get("usdc"), body.get("band")))
        if op == "save":
            try:
                return self._send(200, fv_save(body["fields"], bool(body.get("overwrite"))))
            except (FileExistsError, PermissionError) as e:
                return self._send(409, {"error": str(e)})
        file = body.get("file", "")
        if op == "delete":
            try:
                return self._send(200, fv_delete(file))
            except PermissionError as e:
                return self._send(409, {"error": str(e)})
        f = read_fv(file)
        rel, fid = os.path.relpath(fv_path(file), ROOT), fv_cfg(f).fv_id
        if op == "check":
            return self._send(200, cli(["check", "-c", rel] + [x for x in fv_flags(body) if x != "--clear-halt"]))
        if op == "start":
            dry = bool(body.get("dry"))
            if not dry and not f["live"]:
                return self._send(200, {"ok": False, "out": "ไฟล์นี้ยังไม่อนุญาต live (live_allowed = false) — แก้ในฟอร์มแล้วบันทึกก่อน หรือเริ่มแบบ dry-run"})
            return self._send(200, spawn(rel, f["state_dir"], fid, fv_flags(body), live=not dry))
        if op == "stop":
            return self._send(200, stop_lock(f["state_dir"], fid, note=" — ออเดอร์ของบอทยังค้างบน Deribit (fill ได้ตามปกติ บอทเก็บตกให้ตอนเริ่มใหม่)"))
        if op == "reopen":
            return self._send(200, cli(["reopen", "-c", rel], timeout=20))
        if op == "close":
            if body.get("confirm") != fid:
                return self._send(400, {"error": "พิมพ์รหัสบอทไม่ตรง — ไม่ได้ปิด"})
            if fv_state(f)["running"]:
                return self._send(409, {"error": "หยุดบอทก่อนปิด"})
            return self._send(200, cli(["close", "-c", rel, "--live"]))
        return self._send(404, {"error": "not found"})

    def _kzm_post(self, op: str, body: dict):
        if op == "preview":
            return self._send(200, kzm_preview(body["fields"], body.get("bid"), body.get("ask"), body.get("btc"), body.get("usdc"), body.get("band"), body.get("orders")))
        if op == "save":
            try:
                return self._send(200, kzm_save(body["fields"], bool(body.get("overwrite"))))
            except (FileExistsError, PermissionError) as e:
                return self._send(409, {"error": str(e)})
        file = body.get("file", "")
        if op == "delete":
            try:
                return self._send(200, kzm_delete(file))
            except PermissionError as e:
                return self._send(409, {"error": str(e)})
        f = read_kzm(file)
        rel, fid = os.path.relpath(kzm_path(file), ROOT), kzm_cfg(f).fv_id
        if op == "check":
            return self._send(200, cli(["check", "-c", rel] + [x for x in fv_flags(body) if x != "--clear-halt"]))
        if op == "start":
            dry = bool(body.get("dry"))
            if not dry and not f["live"]:
                return self._send(200, {"ok": False, "out": "ไฟล์นี้ยังไม่อนุญาต live (live_allowed = false) — แก้ในฟอร์มแล้วบันทึกก่อน หรือเริ่มแบบ dry-run"})
            return self._send(200, spawn(rel, f["state_dir"], fid, fv_flags(body), live=not dry))
        if op == "stop":
            return self._send(200, stop_lock(f["state_dir"], fid, note=" — ออเดอร์ของบอทยังค้างบน Deribit (fill ได้ตามปกติ บอทเก็บตกให้ตอนเริ่มใหม่)"))
        if op == "reopen":
            return self._send(200, cli(["reopen", "-c", rel], timeout=20))
        if op == "close":
            if body.get("confirm") != fid:
                return self._send(400, {"error": "พิมพ์รหัสบอทไม่ตรง — ไม่ได้ปิด"})
            if kzm_state(f)["running"]:
                return self._send(409, {"error": "หยุดบอทก่อนปิด"})
            return self._send(200, cli(["close", "-c", rel, "--live"]))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            p = urlparse(self.path).path
            if p == "/api/preview":
                return self._send(200, {"toml": zone_toml(body["fields"])})
            if p.startswith("/api/fv/"):
                return self._fv_post(p[len("/api/fv/"):], body)
            if p.startswith("/api/kzm/"):
                return self._kzm_post(p[len("/api/kzm/"):], body)
            if p == "/api/key/remove":
                try:
                    return self._send(200, remove_key(body.get("acct", "")))
                except PermissionError as e:
                    return self._send(409, {"error": str(e)})
            if p == "/api/tg/token":
                return self._send(200, tg_set_token(body.get("token", "")))
            if p == "/api/tg/chats":
                return self._send(200, {"chats": tg_find_chats()})
            if p == "/api/tg/chat":
                return self._send(200, tg_set_chat(body.get("chat_id", "")))
            if p == "/api/tg/test":
                return self._send(200, tg_test())
            if p == "/api/key/add":
                return self._send(200, add_key(body.get("name", ""), body.get("client_id", ""), body.get("client_secret", ""),
                                               bool(body.get("testnet"))))
            if p == "/api/save":
                f = body["fields"]
                if (f.get("acct") or "") not in {a["acct"] for a in accounts()}:
                    return self._send(400, {"error": f"ไม่มี key ของบัญชี {f.get('acct') or 'หลัก'} ใน .env"})
                path = zone_path(f["file"])
                text = zone_toml(f)
                if os.path.exists(path):
                    if not body.get("overwrite"):
                        return self._send(409, {"error": f"มีไฟล์ zones/{f['file']}.toml อยู่แล้ว"})
                    old = read_zone(f["file"])
                    st = zone_state(old)
                    if st["running"]:
                        return self._send(409, {"error": "บอทของไฟล์นี้กำลังรัน — หยุดบอทก่อนแก้ไฟล์"})
                    bak = os.path.join(ZONES, ".bak")
                    os.makedirs(bak, exist_ok=True)
                    os.replace(path, os.path.join(bak, f"{f['file']}.{time.strftime('%Y%m%d-%H%M%S')}.toml"))
                with open(path, "w", encoding="utf-8") as fo:
                    fo.write(text)
                return self._send(200, {"ok": True, "file": f["file"]})
            f = body.get("file", "")
            rel = os.path.relpath(zone_path(f), ROOT)
            if p == "/api/check":
                return self._send(200, cli(["check", "-c", rel] + ["--adopt-manual"] * bool(body.get("adopt_manual"))))
            if p == "/api/status":
                return self._send(200, cli(["status", "-c", rel], timeout=20))
            if p == "/api/start":
                return self._send(200, start_bot(f, bool(body.get("adopt_manual")), bool(body.get("clear_halt")),
                                                 dry=bool(body.get("dry")), force=bool(body.get("force"))))
            if p == "/api/stop":
                return self._send(200, stop_bot(f))
            if p == "/api/delete":
                try:
                    return self._send(200, delete_zone(f))
                except PermissionError as e:
                    return self._send(409, {"error": str(e)})
            if p == "/api/reopen":
                return self._send(200, cli(["reopen", "-c", rel], timeout=20))
            if p == "/api/close":
                zf = read_zone(f)
                if body.get("confirm") != zone_cfg(zf).zone_id:
                    return self._send(400, {"error": "พิมพ์รหัส zone ไม่ตรง — ไม่ได้ปิด"})
                if zone_state(zf)["running"]:
                    return self._send(409, {"error": "หยุดบอทก่อนปิด zone"})
                return self._send(200, cli(["close", "-c", rel, "--live"]))
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, FileNotFoundError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def serve(port=8777, open_browser=True):
    """โค้ดใน rbsi_bot/ เปลี่ยน → รีสตาร์ทตัวเอง (exec) ที่พอร์ตเดิม token เดิม ตอนไม่มี request ค้าง
    หน้าเว็บที่เปิดอยู่เห็น ver เปลี่ยนแล้วโหลดตัวเองใหม่ — ไม่ต้องปิด/เปิด rbsi-ui.command เอง · บอทที่รันอยู่ไม่กระทบ (เป็น process แยก)"""
    restarted = bool(os.environ.get("RBSI_UI_TOKEN"))
    Handler.token = os.environ.pop("RBSI_UI_TOKEN", None) or secrets.token_urlsafe(24)
    srv = None
    ports = [port] * 20 if restarted else range(port, port + 20)    # รีสตาร์ท: รอพอร์ตเดิมว่าง
    for p in ports:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            time.sleep(0.25 if restarted else 0)
    if srv is None:
        sys.exit(f"ไม่มีพอร์ตว่างช่วง {port}–{port + 19}")
    Handler.port = p
    url = f"http://127.0.0.1:{p}/"
    if restarted:
        print(f"↻ โค้ดอัปเดต — รีสตาร์ทหน้าเว็บแล้ว {url}", flush=True)
    else:
        print(f"RBSI Control เปิดที่ {url}  (เฉพาะเครื่องนี้ · ปิดด้วย Ctrl+C — บอทที่รันอยู่ไม่หยุดตาม)", flush=True)
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    ver0 = code_ver((".py",))

    def watch():
        n = 0
        while True:
            if n % 20 == 0:                                  # ทุก ~30 วิ (และทันทีหลังรีสตาร์ท)
                try:
                    reap_zombies()
                except Exception:
                    pass
            n += 1
            time.sleep(1.5)
            try:
                changed = code_ver((".py",)) != ver0
            except OSError:
                continue
            if changed and _active == 0:
                time.sleep(0.5)                              # เผื่อยังเขียนไฟล์ไม่เสร็จ
                srv.server_close()
                os.environ["RBSI_UI_TOKEN"] = Handler.token
                os.execv(sys.executable, [sys.executable, "-m", "rbsi_bot", "ui", "--port", str(p), "--no-open"])
    threading.Thread(target=watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
