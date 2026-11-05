# -*- coding: utf-8 -*-
"""โหลด config (TOML) + ความลับจาก .env / environment (ไม่เก็บ key ในไฟล์ config)"""
from __future__ import annotations
import os, tomllib
from .grid import ZoneConfig
from .bot import Safety


def load_env(path: str):
    if not path or not os.path.exists(path):
        return
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise PermissionError(f"{path} ต้องอ่านได้เฉพาะเจ้าของ: chmod 600 {path}")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(path: str):
    with open(path, "rb") as f:
        c = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    env_path = os.path.normpath(os.path.join(base, c.get("env_file", "../.env")))
    load_env(env_path)
    z, sf, d, tg = c["zone"], c.get("safety", {}), c.get("deribit", {}), c.get("telegram", {})
    zone = ZoneConfig(instrument=z.get("instrument", "BTC-PERPETUAL"), lower=float(z["lower"]), upper=float(z["upper"]),
                      gap=float(z["gap"]), size=int(z["size"]), tol_frac=float(sf.get("tol_frac", 0.25)),
                      max_catchup=int(sf.get("max_catchup", 2)), chase_frac=float(sf.get("chase_frac", 0.05)),
                      direction=str(z.get("direction", "short")), max_orders=int(z.get("max_orders", 90)))
    safety = Safety(**{k: sf[k] for k in ("settle_sec", "debounce_sec", "flux_halt_sec", "pending_ttl", "reconcile_sec", "liq_buffer", "default_mmr",
                                     "max_orders_per_cycle", "order_spacing_sec", "throttle_max_sec", "heartbeat_sec", "summary_hours") if k in sf})
    return {
        "zone": zone, "safety": safety,
        "testnet": bool(d.get("testnet", False)),
        "client_id": os.environ.get(d.get("client_id_env", "DERIBIT_CLIENT_ID")),
        "client_secret": os.environ.get(d.get("client_secret_env", "DERIBIT_CLIENT_SECRET")),
        "tg_token": os.environ.get(tg.get("token_env", "TG_BOT_TOKEN")),
        "tg_chat": os.environ.get(tg.get("chat_id_env", "TG_CHAT_ID")),
        "state_dir": c.get("state_dir", "~/.rbsi-bot"),
        "live_allowed": bool(c.get("live_allowed", False)),
        "name": c.get("name", ""),
        "env_path": env_path,
    }


def load_fv(path: str):
    """ไฟล์ config ของบอท Fixed Value (มีหมวด [fixed_value] แทน [zone])"""
    from dataclasses import fields
    from .fv import FVConfig
    from .fvbot import FVSafety
    with open(path, "rb") as f:
        c = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    env_path = os.path.normpath(os.path.join(base, c.get("env_file", "../.env")))
    load_env(env_path)
    v, sf, d, tg = c["fixed_value"], c.get("safety", {}), c.get("deribit", {}), c.get("telegram", {})
    cfg = FVConfig(value=float(v["value"]), mode=str(v.get("mode", "usd")), step=float(v["step"]), instrument=str(v.get("instrument", "BTC_USDC")),
                   min_order_usd=float(v.get("min_order_usd", 5)), max_order_usd=float(v.get("max_order_usd", 0)),
                   fee_rate=float(v.get("fee_rate", 0.0002)), chase_bps=float(v.get("chase_bps", 5.0)))
    allowed = {f.name for f in fields(FVSafety)} - {"rebalance_now", "accept_balance"}
    return {
        "kind": "fv", "fv": cfg, "safety": FVSafety(**{k: sf[k] for k in sf if k in allowed}),
        "testnet": bool(d.get("testnet", False)),
        "client_id": os.environ.get(d.get("client_id_env", "DERIBIT_CLIENT_ID")),
        "client_secret": os.environ.get(d.get("client_secret_env", "DERIBIT_CLIENT_SECRET")),
        "tg_token": os.environ.get(tg.get("token_env", "TG_BOT_TOKEN")),
        "tg_chat": os.environ.get(tg.get("chat_id_env", "TG_CHAT_ID")),
        "state_dir": c.get("state_dir", "~/.rbsi-bot"),
        "live_allowed": bool(c.get("live_allowed", False)),
        "name": c.get("name", ""),
        "env_path": env_path,
    }


def is_fv(path: str) -> bool:
    with open(path, "rb") as f:
        return "fixed_value" in tomllib.load(f)


def load_kzm(path: str):
    """ไฟล์ config ของบอท KZM Grid (มีหมวด [kzm]) — ใช้ safety/บัญชี/Telegram ชุดเดียวกับ Fixed Value"""
    from dataclasses import fields
    from .kzm import KZMConfig
    from .fvbot import FVSafety
    with open(path, "rb") as f:
        c = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    env_path = os.path.normpath(os.path.join(base, c.get("env_file", "../.env")))
    load_env(env_path)
    v, sf, d, tg = c["kzm"], c.get("safety", {}), c.get("deribit", {}), c.get("telegram", {})
    cfg = KZMConfig(floor=float(v["floor"]), ceil=float(v["ceil"]), gap=float(v["gap"]), capital=float(v["capital"]),
                    instrument=str(v.get("instrument", "BTC_USDC")), min_order_usd=float(v.get("min_order_usd", 5)),
                    fee_rate=float(v.get("fee_rate", 0.0002)), chase_bps=float(v.get("chase_bps", 5.0)),
                    max_orders=int(v.get("max_orders", 5)), max_chase=int(v.get("max_chase", 3)))
    allowed = {f.name for f in fields(FVSafety)} - {"rebalance_now", "accept_balance"}
    return {
        "kind": "kzm", "fv": cfg, "safety": FVSafety(**{k: sf[k] for k in sf if k in allowed}),
        "testnet": bool(d.get("testnet", False)),
        "client_id": os.environ.get(d.get("client_id_env", "DERIBIT_CLIENT_ID")),
        "client_secret": os.environ.get(d.get("client_secret_env", "DERIBIT_CLIENT_SECRET")),
        "tg_token": os.environ.get(tg.get("token_env", "TG_BOT_TOKEN")),
        "tg_chat": os.environ.get(tg.get("chat_id_env", "TG_CHAT_ID")),
        "state_dir": c.get("state_dir", "~/.rbsi-bot"),
        "live_allowed": bool(c.get("live_allowed", False)),
        "name": c.get("name", ""),
        "env_path": env_path,
    }


def is_kzm(path: str) -> bool:
    with open(path, "rb") as f:
        return "kzm" in tomllib.load(f)
