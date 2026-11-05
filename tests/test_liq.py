# -*- coding: utf-8 -*-
"""สูตร liq ต้องตรงกับ deribit-monitor (ดึงฟังก์ชัน JS มารันใน node) และตรงกับการแก้สมการเชิงตัวเลข"""
import json, os, re, shutil, subprocess
import pytest
from rbsi_bot.liq import liq_short_now, liq_short_after_fills, mm_rate, eq_eff

MON = os.path.expanduser("~/deribit-monitor/index.html")


def bisect_liq(eq, legs, r):
    """แก้ eq + Σ q(1/P − 1/pe) = r·ΣQ/P ตรง ๆ (legs = [(entry, q)])"""
    N = sum(q for _, q in legs)
    f = lambda P: eq + sum(q * (1 / P - 1 / pe) for pe, q in legs) - r * N / P
    lo, hi = min(pe for pe, _ in legs), 10_000_000.0
    for _ in range(300):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if f(m) > 0 else (lo, m)
    return (lo + hi) / 2


def test_now_matches_bisection():
    S, M, eq, r = 3000.0, 83465.9, 0.0012, 0.0126
    # position เข้าที่ mark พอดี (uPnL = 0) → เท่ากับ legs เดียว
    assert liq_short_now(S, M, eq, r) == pytest.approx(bisect_liq(eq, [(M, S)], r), rel=1e-9)


def test_after_fills_matches_bisection():
    S, E, M, eq, r = 3000.0, 84118.2, 83465.9, 0.0012, 0.0126
    adds = [(84000 + 500 * i, 1000.0) for i in range(9)]
    wb = eq + S * (1 / E - 1 / M)
    got = liq_short_after_fills(S, E, M, eq, r, adds)
    assert got == pytest.approx(bisect_liq(wb, [(E, S)] + adds, r), rel=1e-9)


@pytest.mark.skipif(not (os.path.exists(MON) and shutil.which("node")), reason="ไม่มี deribit-monitor หรือ node")
def test_matches_monitor_js():
    src = open(MON, encoding="utf-8").read()
    fns = []
    for name in ("deribitLiqEstimate", "deribitLiqAfterFills"):
        m = re.search(r"function %s\(.*?\n}\n" % name, src, re.S)
        assert m, name
        fns.append(m.group(0))
    p = {"size": -3000, "mark_price": 83465.9, "average_price": 84118.2, "direction": "sell", "maintenance_margin": 0.00045, "instrument_name": "BTC-PERPETUAL"}
    sums = [{"equity": 0.0012, "maintenance_margin": 0.0005}]
    orders = [{"instrument_name": "BTC-PERPETUAL", "direction": "sell", "price": 84000 + 500 * i, "amount": 1000, "filled_amount": 0} for i in range(9)]
    js = "\n".join(fns) + f"\nconst p={json.dumps(p)},s={json.dumps(sums)},o={json.dumps(orders)};" \
         "console.log(JSON.stringify([deribitLiqEstimate(p,'inverse',s,0), deribitLiqAfterFills(p,'inverse',s,o,0)]))"
    a, b = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    S, M, E = 3000.0, 83465.9, 84118.2
    r = mm_rate(0.00045, M, S, 0.0126)
    eq = eq_eff(0.0012, 0.0005, 0.00045)
    assert liq_short_now(S, M, eq, r) == pytest.approx(a, rel=1e-12)
    assert liq_short_after_fills(S, E, M, eq, r, [(o["price"], 1000.0) for o in orders]) == pytest.approx(b, rel=1e-12)
