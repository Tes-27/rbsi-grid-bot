# -*- coding: utf-8 -*-
"""ทดสอบหนัก (ไม่อยู่ในชุด pytest ปกติ): python -m tests.soak [จำนวน seed] [short|long|all]"""
import asyncio, pathlib, sys, tempfile, collections
from tests.test_bot_sim import scenario, Z, ZL

CONFIGS = {
    "no-lag": dict(pos_lag=0, trade_lag=0),
    "lag": dict(pos_lag=3, trade_lag=5),
    "lag+partial+timeout": dict(pos_lag=2, trade_lag=4, timeout_prob=0.15, partial_prob=0.3),
    "jumps+all": dict(pos_lag=2, trade_lag=3, timeout_prob=0.1, partial_prob=0.2, jump=0.08),
    "ออเดอร์มาช้า+ลบประวัติ (7 ต.ค.)": dict(pos_lag=0, trade_lag=1, partial_prob=0.2, order_lag=2, purge_cancelled=True),
    "ออเดอร์มาช้า+ทุกอย่าง": dict(pos_lag=1, trade_lag=3, timeout_prob=0.1, partial_prob=0.2, jump=0.08, order_lag=2, purge_cancelled=True),
    "rate-limit 2/s+lag+timeout": dict(pos_lag=2, trade_lag=3, timeout_prob=0.1, partial_prob=0.2, rate_limit=2, manual_orders=False),
}


async def main(n, which="all"):
    fails = collections.Counter()
    zs = {"short": [("", Z)], "long": [("LONG ", ZL)], "all": [("", Z), ("LONG ", ZL)]}[which]
    for (pre, z), (name, kw) in [(zz, c) for zz in zs for c in CONFIGS.items()]:
        name = pre + name
        for seed in range(n):
            try:
                await scenario(pathlib.Path(tempfile.mkdtemp()), 10_000 + seed, steps=300, z=z, **kw)
            except AssertionError as e:
                fails[name] += 1
                if fails[name] <= 3:
                    print(f"FAIL {name} seed {10_000 + seed}: {str(e)[:200]}")
        print(f"{name}: {n - fails[name]}/{n} ผ่าน")
    return sum(fails.values())

if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100, sys.argv[2] if len(sys.argv) > 2 else "all")) else 0)
