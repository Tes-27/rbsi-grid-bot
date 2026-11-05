# -*- coding: utf-8 -*-
"""หน้าเว็บควบคุม (rbsi_bot/ui.py): TOML ตรวจด้วยกติกาเดียวกับบอท · กันเว็บอื่นยิงเข้ามา · เริ่ม/หยุด process จริง (ใช้บอทปลอมที่ถือ lock)"""
import http.client, json, os, stat, sys, threading, time, tomllib
import pytest
from rbsi_bot import ui
from rbsi_bot.grid import ZoneConfig

BASE = {"name": "T", "file": "t1", "lower": 82000, "upper": 88000, "gap": 500, "size": 1000, "live": True, "testnet": False}


@pytest.fixture
def zones(tmp_path, monkeypatch):
    d = tmp_path / "zones"
    d.mkdir()
    monkeypatch.setattr(ui, "ZONES", str(d))
    return d


def test_toml_roundtrip(zones, tmp_path):
    t = ui.zone_toml({**BASE, "state_dir": str(tmp_path / "st")})
    c = tomllib.loads(t)
    assert c["zone"] == {"instrument": "BTC-PERPETUAL", "direction": "short", "lower": 82000, "upper": 88000, "gap": 500, "size": 1000, "max_orders": 90}
    assert c["live_allowed"] is True and c["env_file"] == "../.env" and c["safety"]["default_mmr"] == 0.0126
    (zones / "t1.toml").write_text(t, encoding="utf-8")
    z = ui.read_zone("t1")
    assert ui.zone_cfg(z).zone_id == ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000).zone_id
    assert tomllib.loads(ui.zone_toml(z)) == c                      # อ่านแล้วเขียนกลับได้ค่าเดิม


def test_toml_roundtrip_long(zones, tmp_path):
    """zone long: direction ถูกเขียน/อ่านกลับ · zone_id ต่อท้าย L (ไม่ชนไฟล์สถานะของ short กรอบเดียวกัน)"""
    t = ui.zone_toml({**BASE, "direction": "long", "state_dir": str(tmp_path / "st")})
    c = tomllib.loads(t)
    assert c["zone"]["direction"] == "long" and "เพดาน long" in t
    (zones / "tl.toml").write_text(t, encoding="utf-8")
    z = ui.read_zone("tl")
    assert z["direction"] == "long" and ui.zone_cfg(z).zone_id == "BTC82000-88000g500q1000L"
    assert tomllib.loads(ui.zone_toml(z)) == c
    with pytest.raises(ValueError):
        ui.zone_toml({**BASE, "direction": "sideways"})
    assert tomllib.loads(ui.zone_toml({**BASE, "max_orders": 60}))["zone"]["max_orders"] == 60
    with pytest.raises(ValueError):
        ui.zone_toml({**BASE, "max_orders": 120})                 # เกินที่ Deribit รับได้


@pytest.mark.parametrize("bad", [{"upper": 88100}, {"size": 1005}, {"gap": 0}, {"lower": 82000.25}, {"tol_frac": 0.6},
                                 {"max_catchup": 0}, {"default_mmr": 0.5}, {"size": 1000.5}])
def test_toml_rejects(bad):
    with pytest.raises(ValueError):
        ui.zone_toml({**BASE, **bad})


def test_name_sanitized():
    c = tomllib.loads(ui.zone_toml({**BASE, "name": 'a"b\nc = 1'}))
    assert c["name"] == "abc = 1"


@pytest.mark.parametrize("f", ["../x", "a/b", ".bak", "", "a b"])
def test_zone_path_rejects(f):
    with pytest.raises(ValueError):
        ui.zone_path(f)


FAKE_BOT = """#!{py}
import fcntl, os, sys, time
# บอทปลอม: เหมือน `python -m rbsi_bot run -c <file> --live` — ถือ lock ของ zone แล้วรอจนโดน SIGINT
p = os.environ["FAKE_LOCK"]
fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("fake bot", " ".join(sys.argv[1:]), flush=True)
try:
    while True:
        time.sleep(0.1)
except KeyboardInterrupt:
    print("stopped by SIGINT", flush=True)
"""


def test_start_stop_cycle(zones, tmp_path, monkeypatch):
    st = tmp_path / "st"
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "state_dir": str(st)}), encoding="utf-8")
    zid = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000).zone_id
    lp = str(st / f"{zid}.lock")
    fake = tmp_path / "fakepy"
    fake.write_text(FAKE_BOT.format(py=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ui.sys, "executable", str(fake))
    monkeypatch.setenv("FAKE_LOCK", lp)

    r = ui.start_bot("t1", adopt_manual=True, clear_halt=False, wait=6)
    assert r["ok"], r
    assert "-m rbsi_bot run -c" in r["out"] and "--live --adopt-manual" in r["out"]
    assert ui.is_locked(lp) and ui.lock_pids(lp) == [r["pid"]]
    assert not ui.start_bot("t1", False, False)["ok"]                # รันซ้อนไม่ได้
    s = ui.zone_state(ui.read_zone("t1"))
    assert s["running"] and s["pids"] == [r["pid"]]

    r2 = ui.stop_bot("t1", wait=5)
    assert r2["ok"] and not ui.is_locked(lp)
    with open(st / f"{zid}.out", encoding="utf-8") as f:
        assert "stopped by SIGINT" in f.read()                     # หยุดแบบ Ctrl+C ไม่ใช่ kill
    assert ui.stop_bot("t1")["out"].startswith("ไม่มีบอท")


def test_start_returns_as_soon_as_bot_started(zones, tmp_path, monkeypatch):
    """เดิมรอตายตัว ≥ 3.5 วิ · ตอนนี้ตอบทันทีที่บอทถือ lock + พิมพ์ "▶️ เริ่มบอท" (ผ่านด่านตอนเริ่มแล้ว)"""
    st = tmp_path / "st"
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "state_dir": str(st)}), encoding="utf-8")
    lp = str(st / f"{ZoneConfig('BTC-PERPETUAL', 82000, 88000, 500, 1000).zone_id}.lock")
    fake = tmp_path / "fakepy"
    fake.write_text(FAKE_BOT.format(py=sys.executable).replace('print("fake bot"', 'print("▶️ เริ่มบอท LIVE", flush=True)\nprint("fake bot"'))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ui.sys, "executable", str(fake))
    monkeypatch.setenv("FAKE_LOCK", lp)
    t0 = time.time()
    r = ui.start_bot("t1", adopt_manual=False, clear_halt=False, wait=6)
    took = time.time() - t0
    try:
        assert r["ok"] and "▶️ เริ่มบอท" in r["out"] and took < 2, (took, r)
    finally:
        assert ui.stop_bot("t1", wait=5)["ok"]


def test_zone_state_calls_lsof_once(zones, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ui, "is_locked", lambda p: True)
    monkeypatch.setattr(ui, "lock_pids", lambda p: calls.append(p) or [123])
    monkeypatch.setattr(ui, "pid_mode", lambda pids: "live" if pids == [123] else None)
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "state_dir": str(tmp_path / "st")}), encoding="utf-8")
    s = ui.zone_state(ui.read_zone("t1"))
    assert s["pids"] == [123] and s["mode"] == "live" and len(calls) == 1


def test_start_refuses_without_live(zones, tmp_path):
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "live": False, "state_dir": str(tmp_path)}), encoding="utf-8")
    r = ui.start_bot("t1", False, False)
    assert not r["ok"] and "live_allowed" in r["out"]


def test_http_guard(zones, tmp_path, envfile):
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "state_dir": str(tmp_path)}), encoding="utf-8")
    ui.Handler.token = "tok123"
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    ui.Handler.port = port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def req(method, path, headers=None, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read().decode()
        st, html = req("GET", "/")
        assert st == 200 and "'tok123'" in html
        assert req("GET", "/api/zones")[0] == 403                                       # ไม่มี token
        assert req("GET", "/api/zones", {"X-Token": "tok123", "Host": "evil.example"})[0] == 403   # DNS rebinding
        st, body = req("GET", "/api/zones", {"X-Token": "tok123"})
        assert st == 200 and json.loads(body)["zones"][0]["file"] == "t1"
        st, body = req("POST", "/api/save", {"X-Token": "tok123"}, {"fields": {**BASE, "state_dir": str(tmp_path)}})
        assert st == 409                                                                # มีไฟล์อยู่แล้ว ต้องยืนยันเขียนทับ
        st, body = req("POST", "/api/save", {"X-Token": "tok123"}, {"fields": {**BASE, "size": 500, "state_dir": str(tmp_path)}, "overwrite": True})
        assert st == 200 and ui.read_zone("t1")["size"] == 500
        assert len(os.listdir(zones / ".bak")) == 1                                    # ไฟล์เดิมสำรองไว้
        st, body = req("POST", "/api/close", {"X-Token": "tok123"}, {"file": "t1", "confirm": "wrong"})
        assert st == 400
    finally:
        srv.shutdown()


def test_reopen_closed_zone(tmp_path):
    from rbsi_bot.store import Store
    st = Store(str(tmp_path), "Z1")
    with pytest.raises(ValueError):
        st.archive_closed()                                     # ยังไม่ปิด เปิดใหม่ไม่ได้
    st.data.update(status="closed", baseline={"1": 1000.0})
    st.save()
    dst = st.archive_closed()
    assert os.path.exists(dst) and ".closed-" in dst and json.load(open(dst))["status"] == "closed"
    assert st.data["status"] == "new" and st.data["baseline"] == {}
    assert Store(str(tmp_path), "Z1").data["status"] == "new"


def test_delete_zone_moves_to_bak_and_refuses_running(zones, tmp_path):
    import fcntl
    st = tmp_path / "st"
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "state_dir": str(st)}), encoding="utf-8")
    (zones / "t2.toml").write_text(ui.zone_toml({**BASE, "file": "t2", "state_dir": str(st)}), encoding="utf-8")
    zid = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000).zone_id
    os.makedirs(st, exist_ok=True)
    fd = os.open(st / f"{zid}.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)                                   # จำลองบอทรันอยู่
    try:
        with pytest.raises(PermissionError):
            ui.delete_zone("t1")
    finally:
        os.close(fd)
    r = ui.delete_zone("t1")
    assert not (zones / "t1.toml").exists() and (zones / "t2.toml").exists()
    assert r["moved_to"].endswith(".toml") and ".deleted-" in r["moved_to"]
    assert len([f for f in os.listdir(zones / ".bak") if f.startswith("t1.deleted-")]) == 1
    with pytest.raises(FileNotFoundError):
        ui.delete_zone("t1")
    with pytest.raises(ValueError):
        ui.delete_zone("../t2")


# ---------- หลาย subaccount ----------
@pytest.fixture
def envfile(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("DERIBIT_CLIENT_ID=mainid1234\nDERIBIT_CLIENT_SECRET=mainsecret123456\nTG_BOT_TOKEN=x\n", encoding="utf-8")
    p.chmod(0o600)
    monkeypatch.setattr(ui, "ENV_PATH", str(p))
    return p


def test_accounts_and_add_key(envfile):
    assert [a["acct"] for a in ui.accounts()] == [""]
    seen = {}
    async def ok(testnet, cid, secret):
        seen.update(cid=cid, testnet=testnet)
        return {"user": "Beta3", "type": "subaccount"}
    r = ui.add_key("Beta 3", " beta3id99 ", "beta3secret_abcdef", verify=ok)
    assert r["acct"] == "BETA_3" and r["user"] == "Beta3" and seen == {"cid": "beta3id99", "testnet": False}
    env = ui.read_env()
    assert env["DERIBIT_CLIENT_ID_BETA_3"] == "beta3id99" and env["DERIBIT_CLIENT_SECRET_BETA_3"] == "beta3secret_abcdef"
    assert env["DERIBIT_CLIENT_ID"] == "mainid1234" and env["TG_BOT_TOKEN"] == "x"          # ของเดิมไม่หาย
    assert oct(envfile.stat().st_mode & 0o777) == "0o600"
    accts = ui.accounts()
    assert [a["acct"] for a in accts] == ["", "BETA_3"] and accts[1]["name"] == "Beta 3" and all(a["ready"] for a in accts)
    assert "secret" not in json.dumps(accts).lower().replace("secret_env", "")
    with pytest.raises(ValueError, match="อยู่แล้ว"):
        ui.add_key("beta-3", "otherid999", "othersecret_abcdef", verify=ok)             # ชื่อซ้ำ
    with pytest.raises(ValueError, match="มีอยู่แล้ว"):
        ui.add_key("Beta4", "mainid1234", "othersecret_abcdef", verify=ok)             # key ซ้ำ


def test_add_key_not_saved_when_login_fails(envfile):
    before = envfile.read_text()
    async def bad(*a):
        raise RuntimeError("invalid_credentials")
    with pytest.raises(ValueError, match="login Deribit ไม่ผ่าน"):
        ui.add_key("Beta5", "beta5id99", "beta5secret_abcdef", verify=bad)
    with pytest.raises(ValueError, match="รูปแบบ"):
        ui.add_key("Beta5", "id with space", "beta5secret_abcdef", verify=bad)
    assert envfile.read_text() == before


def test_zone_bound_to_account(zones, envfile, tmp_path):
    t = ui.zone_toml({**BASE, "acct": "BETA3", "state_dir": str(tmp_path)})
    c = tomllib.loads(t)
    assert c["deribit"]["client_id_env"] == "DERIBIT_CLIENT_ID_BETA3" and c["deribit"]["client_secret_env"] == "DERIBIT_CLIENT_SECRET_BETA3"
    (zones / "t1.toml").write_text(t, encoding="utf-8")
    assert ui.read_zone("t1")["acct"] == "BETA3"
    (zones / "t2.toml").write_text(ui.zone_toml({**BASE, "file": "t2", "state_dir": str(tmp_path)}), encoding="utf-8")
    assert ui.read_zone("t2")["acct"] == ""
    with pytest.raises(ValueError):
        ui.zone_toml({**BASE, "acct": "../x"})


def test_account_reads_key_of_selected_tab(envfile, monkeypatch):
    """/api/account ต้องใช้ key ของแท็บที่เลือก (เคยพัง: ลบฟังก์ชันดึงบัญชีไปตอนแก้)"""
    used = []
    class FakeClient:
        def __init__(self, testnet, cid, secret):
            used.append(cid)
        async def connect(self): pass
        async def close(self): pass
        async def ticker(self, ins): return {"mark_price": 84000.0}
        async def rpc(self, m, p=None): return {"username": "Sub-" + used[-1], "equity": 0.01}
        async def position(self, ins): return {"size": -1000.0}
        async def open_orders(self, ins): return []
    monkeypatch.setattr(ui, "DeribitClient", FakeClient)
    async def ok(*a): return {"user": "x"}
    ui.add_key("Beta3", "beta3id99", "beta3secret_abcdef", verify=ok)
    ui._acct_cache.clear()
    a = ui.account("BETA3", False)
    b = ui.account("", False)
    assert used == ["beta3id99", "mainid1234"] and a["user"] == "Sub-beta3id99" and a["acct"] == "BETA3" and b["position"] == -1000.0


# ---------- แผงบอท Fixed Value ----------
FVB = {"acct": "", "name": "FVT", "file": "f1", "value": 3500, "mode": "usd", "step": 100, "min_order_usd": 5, "max_order_usd": 1000, "live": True}


@pytest.fixture
def fvdir(tmp_path, monkeypatch):
    d = tmp_path / "fv"
    d.mkdir()
    monkeypatch.setattr(ui, "FV_DIR", str(d))
    return d


def test_fv_toml_roundtrip_with_bot_loader(fvdir, envfile, tmp_path):
    from rbsi_bot.config import load_fv
    t = ui.fv_toml({**FVB, "acct": "BETA3", "mode": "pct", "step": 2.5, "state_dir": str(tmp_path / "st")})
    (fvdir / "f1.toml").write_text(t, encoding="utf-8")
    C = load_fv(str(fvdir / "f1.toml"))                                   # ตัวโหลดของบอทจริงอ่านได้
    assert C["fv"].value == 3500 and C["fv"].mode == "pct" and C["fv"].delta == 87.5 and C["live_allowed"] is True
    f = ui.read_fv("f1")
    assert f["acct"] == "BETA3" and f["step"] == 2.5 and f["max_order_usd"] == 1000
    assert tomllib.loads(ui.fv_toml(f)) == tomllib.loads(t)                 # อ่านแล้วเขียนกลับได้ค่าเดิม
    for bad in ({"value": 0}, {"step": 5000}, {"mode": "x"}, {"acct": "../x"}, {"fee_rate": 0.5}):
        with pytest.raises(ValueError):
            ui.fv_toml({**FVB, **bad})


def test_fv_save_list_delete(fvdir, envfile, tmp_path):
    (fvdir / "example-fixed-value.toml").write_text(ui.fv_toml({**FVB, "acct": "NOKEY"}), encoding="utf-8")
    f = {**FVB, "state_dir": str(tmp_path / "st")}
    assert ui.fv_save(f, False)["ok"]
    with pytest.raises(FileExistsError):
        ui.fv_save(f, False)
    assert ui.fv_save({**f, "step": 50}, True)["ok"] and ui.read_fv("f1")["step"] == 50
    assert len(os.listdir(fvdir / ".bak")) == 1
    with pytest.raises(ValueError, match="ไม่มี key"):
        ui.fv_save({**f, "file": "f2", "acct": "ZZZ"}, False)
    bots = ui.list_fv()
    assert [b["file"] for b in bots] == ["f1"] and bots[0]["state"]["status"] == "new"   # ไม่รวมไฟล์ตัวอย่าง
    r = ui.fv_delete("f1")
    assert "deleted" in r["moved_to"] and ui.list_fv() == []


def test_fv_preview_far_from_target_uses_post_rebalance_state():
    # BTC $556 USDC 0 เป้า $3,500 → ต้องซื้อเกือบ $2,944 แต่ไม่มี USDC
    r = ui.fv_preview(FVB, 84063.0, 84063.01, 0.0066178, 0.0)
    assert r["after"] and r["cash_short"] and r["dev"] == pytest.approx(556.31 - 3500, abs=0.05)
    assert r["sell_at"] == pytest.approx(84063.005 * 3600 / 3500, rel=1e-4)            # ราคาขายถัดไปคิดจากยอดหลังปรับ ไม่ใช่ 540,000+
    assert r["buy"][0] == 84063.0 and r["buy"][1] * r["buy"][0] <= 1000.01              # ไม้ปรับเข้าเป้าที่ ask−tick จำกัด $1,000
    # อยู่ในกรอบ → ใช้ยอดจริง · USDC 0 = หมดตั้งแต่ไม้ซื้อแรก
    r = ui.fv_preview({**FVB, "value": 560, "step": 20}, 84063.0, 84063.01, 0.0066178, 0.0)
    assert not r["after"] and r["cash_out"] == pytest.approx(r["buy_at"], abs=0.02)
    assert r["round_trip"] == pytest.approx(20 ** 2 / 560 - 2 * 20 * 0.0002)


def test_fv_routes_start_stop(fvdir, envfile, tmp_path, monkeypatch):
    st = tmp_path / "st"
    ui.fv_save({**FVB, "state_dir": str(st)}, False)
    fid = ui.fv_cfg(ui.read_fv("f1")).fv_id
    fake = tmp_path / "fakepy"
    fake.write_text(FAKE_BOT.format(py=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ui.sys, "executable", str(fake))
    monkeypatch.setenv("FAKE_LOCK", str(st / f"{fid}.lock"))
    ui.Handler.token = "tok9"
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    ui.Handler.port = port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            c.request("POST", path, body=json.dumps(body), headers={"X-Token": "tok9"})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        s, r = post("/api/fv/start", {"file": "f1", "rebalance_now": True})
        assert s == 200 and r["ok"], r
        assert "run -c" in r["out"] and "--live --rebalance-now" in r["out"]
        assert ui.fv_state(ui.read_fv("f1"))["running"]
        assert post("/api/fv/save", {"fields": {**FVB, "state_dir": str(st)}, "overwrite": True})[0] == 409   # แก้ไฟล์ตอนรันไม่ได้
        assert post("/api/fv/delete", {"file": "f1"})[0] == 409
        assert post("/api/fv/close", {"file": "f1", "confirm": fid})[0] == 409                               # ต้องหยุดก่อน
        s, r = post("/api/fv/stop", {"file": "f1"})
        assert r["ok"] and not ui.fv_state(ui.read_fv("f1"))["running"]
        assert post("/api/fv/close", {"file": "f1", "confirm": "ผิด"})[0] == 400
    finally:
        srv.shutdown()


# ---------- ตั้งค่า .env / Telegram / ลบ key / dry-run / error ตอนบอทตาย ----------
def test_env_update_keeps_other_lines_and_backs_up(envfile):
    envfile.write_text("# คอมเมนต์\nDERIBIT_CLIENT_ID=mainid1234\nDERIBIT_CLIENT_SECRET=mainsecret123456\nTG_BOT_TOKEN=old\n", encoding="utf-8")
    envfile.chmod(0o600)
    ui.env_update({"TG_BOT_TOKEN": "new", "TG_CHAT_ID": "555"}, remove={"DERIBIT_CLIENT_SECRET"})
    t = envfile.read_text()
    assert t.startswith("# คอมเมนต์\nDERIBIT_CLIENT_ID=mainid1234\n") and "TG_BOT_TOKEN=new" in t and "TG_CHAT_ID=555" in t
    assert "DERIBIT_CLIENT_SECRET" not in t and oct(envfile.stat().st_mode & 0o777) == "0o600"
    baks = [p for p in envfile.parent.iterdir() if p.name.startswith(".env.bak-")]
    assert len(baks) == 1 and "TG_BOT_TOKEN=old" in baks[0].read_text() and oct(baks[0].stat().st_mode & 0o777) == "0o600"
    for bad in ("a b", 'x"y', "x#y"):
        with pytest.raises(ValueError):
            ui.env_update({"TG_CHAT_ID": bad})


def test_remove_key_refuses_while_running(envfile, zones, fvdir, tmp_path):
    import fcntl
    async def ok(*a): return {"user": "x"}
    ui.add_key("Beta3", "beta3id99", "beta3secret_abcdef", verify=ok)
    st = tmp_path / "st"
    (zones / "z3.toml").write_text(ui.zone_toml({**BASE, "file": "z3", "acct": "BETA3", "state_dir": str(st)}), encoding="utf-8")
    zid = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000).zone_id
    st.mkdir(exist_ok=True)
    fd = os.open(st / f"{zid}.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(PermissionError, match="z3"):
            ui.remove_key("BETA3")
    finally:
        os.close(fd)
    r = ui.remove_key("BETA3")
    assert r["files_left"] == ["z3"] and [a["acct"] for a in ui.accounts()] == [""]
    assert "BETA3" not in envfile.read_text() and "mainid1234" in envfile.read_text()


def test_telegram_settings(envfile, monkeypatch):
    calls = []
    def fake_tg(token, method, params=None, timeout=10):
        calls.append((token, method, params))
        if method == "getMe":
            return {"username": "MyBot", "first_name": "Bot"}
        if method == "getUpdates":
            return [{"message": {"chat": {"id": 12345678, "first_name": "Setto"}}}]
        return True
    monkeypatch.setattr(ui, "_tg", fake_tg)
    ui._tg_cache.clear()
    envfile.write_text("DERIBIT_CLIENT_ID=mainid1234\nDERIBIT_CLIENT_SECRET=mainsecret123456\n", encoding="utf-8")   # ยังไม่เคยตั้ง Telegram
    assert ui.tg_status() == {"has_token": False, "has_chat": False, "chat_hint": ""}
    with pytest.raises(ValueError, match="รูปแบบ"):
        ui.tg_set_token("nope")
    tok = "123456789:" + "A" * 35
    assert ui.tg_set_token(tok)["bot"] == "@MyBot"
    assert ui.tg_find_chats() == [{"id": "12345678", "name": "Setto"}]
    ui.tg_set_chat("12345678")
    st = ui.tg_status()
    assert st["bot"] == "@MyBot" and st["chat_hint"] == "…5678" and tok not in json.dumps(st)        # token ไม่หลุดออกไป
    ui.tg_test()
    assert calls[-1][1] == "sendMessage" and calls[-1][2]["chat_id"] == "12345678"


def test_tg_error_masks_token(monkeypatch):
    import urllib.request
    tok = "123456789:" + "B" * 35
    def boom(*a, **k):
        raise OSError(f"cannot reach https://api.telegram.org/bot{tok}/getMe")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ValueError) as e:
        ui._tg(tok, "getMe")
    assert tok not in str(e.value) and "<token>" in str(e.value)


def test_crash_info(tmp_path):
    out = tmp_path / "Z.out"
    out.write_text("\n===== 2026-09-29 10:00:00 เริ่มจากหน้าเว็บ: run\nlog...\nTraceback (most recent call last):\n  x\nKeyError: 'boom'\n", encoding="utf-8")
    c = ui.crash_info(str(tmp_path), "Z")
    assert c and c["error"] == "KeyError: 'boom'"
    out.write_text(out.read_text() + "\n===== 2026-09-29 11:00:00 เริ่มจากหน้าเว็บ: run\nจบ: halted\n", encoding="utf-8")
    assert ui.crash_info(str(tmp_path), "Z") is None                                   # รอบล่าสุดจบปกติ
    out.write_text(out.read_text() + "\n===== 2026-09-29 12:00:00 เริ่มจากหน้าเว็บ: run\nTraceback\nKeyboardInterrupt\n", encoding="utf-8")
    assert ui.crash_info(str(tmp_path), "Z") is None                                   # Ctrl+C ไม่นับ


def test_start_dry_run_and_force(zones, tmp_path, monkeypatch):
    st = tmp_path / "st"
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "live": False, "state_dir": str(st)}), encoding="utf-8")
    zid = ZoneConfig("BTC-PERPETUAL", 82000, 88000, 500, 1000).zone_id
    fake = tmp_path / "fakepy"
    fake.write_text(FAKE_BOT.format(py=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ui.sys, "executable", str(fake))
    monkeypatch.setenv("FAKE_LOCK", str(st / f"{zid}.lock"))
    assert not ui.start_bot("t1", False, False)["ok"]                                  # live_allowed = false → live ไม่ได้
    r = ui.start_bot("t1", True, False, wait=6, dry=True, force=True)
    assert r["ok"] and "--live" not in r["out"] and "--force" not in r["out"] and "--adopt-manual" in r["out"]
    s = ui.zone_state(ui.read_zone("t1"))
    assert s["running"] and s["mode"] == "dry"
    assert ui.stop_bot("t1", wait=5)["ok"]
    assert "หยุดจากหน้าเว็บ" in (st / f"{zid}.out").read_text() and ui.zone_state(ui.read_zone("t1"))["crash"] is None
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "live": True, "state_dir": str(st)}), encoding="utf-8")
    r = ui.start_bot("t1", False, False, wait=6, force=True)
    assert r["ok"] and "--live --force" in r["out"] and ui.zone_state(ui.read_zone("t1"))["mode"] == "live"
    ui.stop_bot("t1", wait=5)


def test_fv_preview_band_and_reopen(fvdir, envfile, tmp_path):
    r = ui.fv_preview({**FVB, "mode": "pct", "step": 2, "value": 5000}, 84063.0, 84063.01, 0.05921256, 5021.39, band=[82695.57, 86070.91])
    assert r["sell"][0] > 86070.91 and r["out_of_band"] == {"sell": True, "buy": False}   # ขาย 2% อยู่นอกกรอบ Deribit (เจอจริง)
    from rbsi_bot.fvbot import FVStore
    st = tmp_path / "st"
    ui.fv_save({**FVB, "state_dir": str(st)}, False)
    fid = ui.fv_cfg(ui.read_fv("f1")).fv_id
    s = FVStore(str(st), fid)
    s.data["status"] = "closed"
    s.save()
    out = ui.cli(["reopen", "-c", str(fvdir / "f1.toml")], timeout=30)
    assert out["ok"], out["out"]
    assert ui.fv_state(ui.read_fv("f1"))["status"] == "new" and any(".closed-" in p.name for p in st.iterdir())


# ---------- บอท KZM Grid ----------
KZB = {"name": "KZ", "file": "k1", "floor": 15000, "ceil": 150000, "gap": 500, "capital": 10000, "acct": "", "live": True, "testnet": False}


@pytest.fixture
def kzdir(tmp_path, monkeypatch):
    d = tmp_path / "kzm"
    d.mkdir()
    monkeypatch.setattr(ui, "KZM_DIR", str(d))
    return d


def test_kzm_toml_roundtrip_save_list_delete(kzdir, envfile, tmp_path):
    from rbsi_bot.config import load_kzm, is_kzm, is_fv
    t = ui.kzm_toml({**KZB, "acct": "BETA3", "max_orders": 4, "state_dir": str(tmp_path / "st")})
    (kzdir / "x.toml").write_text(t, encoding="utf-8")
    C = load_kzm(str(kzdir / "x.toml"))                                     # ตัวโหลดของบอทจริงอ่านได้
    assert is_kzm(str(kzdir / "x.toml")) and not is_fv(str(kzdir / "x.toml"))
    assert C["kind"] == "kzm" and C["fv"].n == 270 and C["fv"].max_orders == 4 and C["live_allowed"] is True
    f = ui.read_kzm("x")
    assert f["acct"] == "BETA3" and tomllib.loads(ui.kzm_toml(f)) == tomllib.loads(t)
    for bad in ({"gap": 700}, {"capital": 100}, {"ceil": 10000}, {"acct": "../x"}, {"max_orders": 50}):
        with pytest.raises(ValueError):
            ui.kzm_toml({**KZB, **bad})
    os.remove(kzdir / "x.toml")
    f = {**KZB, "state_dir": str(tmp_path / "st")}
    assert ui.kzm_save(f, False)["ok"]
    with pytest.raises(FileExistsError):
        ui.kzm_save(f, False)
    assert ui.kzm_save({**f, "gap": 1000}, True)["ok"] and ui.read_kzm("k1")["gap"] == 1000
    bots = ui.list_kzm()
    assert [b["file"] for b in bots] == ["k1"] and bots[0]["state"]["status"] == "new" and bots[0]["state"]["n"] == 135
    assert "deleted" in ui.kzm_delete("k1")["moved_to"] and ui.list_kzm() == []


def test_kzm_preview_ratio_start(tmp_path):
    f = {**KZB, "state_dir": str(tmp_path / "st")}
    r = ui.kzm_preview(f, 84000.0, 84000.01, 0.0, 10000.0, [82320, 85680])
    s = r["start"]
    assert s["layers_ideal"] == 131 and s["buy_usd"] == pytest.approx(131 * 10000 / 270, rel=1e-3)     # ชั้นละ $cost ที่ราคาเริ่ม (ตาม Excel)
    assert r["share_after"] == pytest.approx(0.485, abs=0.005) and not r["started"]
    assert r["surplus"] == pytest.approx(10000 - s["buy_usd"] * 1.0002 - r["to_floor"], abs=0.01)
    placed = [a for a in r["actions"]]
    assert len([a for a in placed if a[2] == 84000.0 and a[1] != "0138b"]) == 3                       # ไล่ราคาซื้อชั้นตั้งต้นทีละ 3
    assert "0134b" in r["waiting"]                                                                     # 82,000 นอกกรอบ Deribit → รอ
    r2 = ui.kzm_preview(f, 84000.0, 84000.01, 1.0, 0.0)
    assert "error_btc" in r2


def test_reap_zombies_after_ui_restart():
    """บอทที่เริ่มก่อนหน้าเว็บรีสตาร์ท (execv) ไม่มี thread รอเก็บ → จบแล้วค้างเป็น zombie · reap_zombies เก็บให้ แต่ไม่แย่งตัวที่ spawn() ดูแล"""
    import subprocess
    orphan = subprocess.Popen([sys.executable, "-c", "pass"])          # จำลองลูกจาก image ก่อนรีสตาร์ท (ไม่มีใคร wait)
    mine = subprocess.Popen([sys.executable, "-c", "pass"])
    ui._spawned.add(mine.pid)
    try:
        deadline = time.time() + 5
        got = []
        while time.time() < deadline and orphan.pid not in got:
            time.sleep(0.1)
            got += ui.reap_zombies()
        assert orphan.pid in got and mine.pid not in got
        assert mine.wait(timeout=5) == 0                                  # ตัวที่ spawn() ดูแลยังเก็บเองได้ปกติ
    finally:
        ui._spawned.discard(mine.pid)


def test_api_transfer_uses_zone_account(zones, tmp_path, envfile, monkeypatch):
    """/api/transfer: ดึงบัญชีของ zone (key ตาม acct) แล้วคำนวณโอนออกได้ · ส่ง withdrawable / pos_im มาด้วย"""
    class FakeClient:
        def __init__(self, testnet, cid, secret): pass
        async def connect(self): pass
        async def close(self): pass
        async def ticker(self, ins): return {"mark_price": 85000.0}
        async def rpc(self, m, p=None): return {"username": "u", "equity": 0.05, "available_funds": 0.004, "available_withdrawal_funds": 0.003}
        async def position(self, ins): return {"size": -2000.0, "initial_margin": 2000 / 85000 * 0.02}
        async def open_orders(self, ins): return []
    monkeypatch.setattr(ui, "DeribitClient", FakeClient)
    ui._acct_cache.clear()
    (zones / "t1.toml").write_text(ui.zone_toml({**BASE, "state_dir": str(tmp_path)}), encoding="utf-8")
    ui.Handler.token = "tok123"
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    ui.Handler.port = port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/transfer?file=t1", headers={"X-Token": "tok123"})
        r = c.getresponse()
        x = json.loads(r.read().decode())
        assert r.status == 200 and x["withdrawable"] == 0.003 and 0 < x["transferable"] <= 0.003
        assert x["im_rate"] == pytest.approx(0.02)
    finally:
        srv.shutdown()
