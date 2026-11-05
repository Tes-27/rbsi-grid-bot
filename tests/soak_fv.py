# -*- coding: utf-8 -*-
"""ทดสอบหนักบอท Fixed Value (ไม่อยู่ใน pytest ปกติ): python -m tests.soak_fv [จำนวน seed]"""
import asyncio, pathlib, sys, tempfile
from tests.test_fv import scenario

CONFIGS = {
    "ปกติ": {},
    "lag+partial+timeout": dict(bal_lag=2, trade_lag=3, partial_prob=0.3, timeout_prob=0.15),
    "กระโดด+ทุกอย่าง": dict(jump=0.05, bal_lag=2, trade_lag=3, partial_prob=0.3, timeout_prob=0.1),
}

if __name__ == "__main__":
    n, bad = int(sys.argv[1]) if len(sys.argv) > 1 else 100, 0
    for name, kw in CONFIGS.items():
        f = fills = 0
        for seed in range(n):
            try:
                _, _, store, _ = asyncio.run(scenario(pathlib.Path(tempfile.mkdtemp()), 1000 + seed, **kw))
                fills += len(store.data["fills"])
            except AssertionError as e:
                f += 1
                print(f"FAIL {name} seed {1000 + seed}: {str(e)[:200]}")
        bad += f
        print(f"{name}: {n - f}/{n} ผ่าน · fill รวม {fills:,}")
    sys.exit(1 if bad else 0)
