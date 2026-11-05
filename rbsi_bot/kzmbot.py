# -*- coding: utf-8 -*-
"""บอท KZM Grid บน Deribit spot (BTC_USDC) — ใช้โครงบอท Fixed Value ทั้งหมด (อ่าน 2 รอบ, บัญชีไม้, flux/halt, คำตอบหาย, Telegram)
ต่างกันที่: ออเดอร์ติด label รายชั้น kz_<id>_<ชั้น><s|b> · บัญชีไม้รายชั้น h_k (+ ต้นทุนของชั้น) · ตัดสินใจด้วย kzm.plan
subaccount แยก: บอทถือว่า BTC/USDC ทั้งหมดใน sub เป็นของกริด — BTC ที่มีอยู่ตอนเริ่มถูกนับเข้าชั้น (ชั้นที่ควรถือก่อน)
Fix Cash ต่อชั้น (ตาม Excel Grid_KZM): ซื้อชั้นละ capital/n เสมอ ขายทั้งจำนวนของชั้นที่ +gap → ส่วนต่าง = cashflow สะสมนอกกริด
  บอทไม่ปรับสัดส่วนตามราคาหลังเริ่ม (ratio ใช้แค่ตอนเริ่ม) · เงินสดส่วนเกิน = USDC − ที่ต้องกันไว้ซื้อทุกชั้นที่ยังไม่เต็มจนถึง floor
กำไรแยก 2 ก้อน (บทเรียน KZM: กำไรส่วนใหญ่มาจาก capital gain ไม่ใช่กริด):
  ขายชั้นตั้งต้น (ชั้นที่ถือตั้งแต่เริ่มบอท) = capital gain · ขายชั้นที่ซื้อจากกริด = กำไรรอบกริดแท้"""
from __future__ import annotations
import logging

from .fvbot import FVBot, FVStore, FVSafety, Refuse
from .kzm import KZMConfig, plan, parse_label, parse_key, allocate, deallocate, ideal_held, start_plan, cash_to_floor, start_cap

log = logging.getLogger("kzmbot")

__all__ = ["KZMBot", "KZMStore", "FVSafety", "Refuse"]


class KZMStore(FVStore):
    def _load(self):
        d = super()._load()
        if d.get("kind") == "fv" and d.get("status") == "new" and "layers" not in d:
            d["kind"] = "kzm"
        d.setdefault("layers", {})            # "k" -> {"h": BTC, "c": ต้นทุน USD ของ BTC ที่ถืออยู่}
        d.setdefault("init", [])              # ชั้นตั้งต้น (ขาย = capital gain)
        d.setdefault("cap", {})               # "k" -> BTC เต็มชั้นของชั้นตั้งต้น (cost / ราคาเริ่ม) · ขายหมดแล้วกลับเป็น cost / ราคาชั้น
        d.setdefault("grid_pnl", 0.0)
        d.setdefault("init_pnl", 0.0)
        d.setdefault("rounds", 0)
        return d


class KZMBot(FVBot):
    cfg: KZMConfig

    # ---------- ตัวตนของออเดอร์ ----------
    def is_own(self, label, order_id):
        return parse_label(self.cfg, label) or self.store.data["known"].get(order_id)

    def direction(self, key):
        return parse_key(key)[1]

    def title(self):
        c = self.cfg
        return f"KZM Grid {c.instrument} {c.floor:,.0f}–{c.ceil:,.0f} gap {c.gap:,.0f} ทุน ${c.capital:,.0f} ({c.n} ชั้น × ${c.cost:,.2f})"

    def describe(self, a):
        if a[0] == "cancel":
            k, side = parse_key(a[2]) or (-1, "?")
            return f"ยกเลิก ชั้น {k} {side} {a[1]} ({a[3]})"
        _, kk, price, amt = a
        k, side = parse_key(kk)
        return f"{'SELL' if side == 'sell' else 'BUY'} ชั้น {k} {amt:.8f} {self.cfg.base} @ {price:,.2f} (${amt * price:,.2f})"

    # ---------- บัญชีไม้รายชั้น ----------
    def h(self) -> dict:
        return {int(k): float(v["h"]) for k, v in self.store.data["layers"].items() if float(v["h"]) > 0}

    def cap(self) -> dict:
        return {int(k): float(v) for k, v in self.store.data["cap"].items()}

    def _layer(self, k):
        return self.store.data["layers"].setdefault(str(k), {"h": 0.0, "c": 0.0})

    def add_btc(self, amounts: dict, px: float, init: bool):
        d = self.store.data
        for k, x in amounts.items():
            L = self._layer(k)
            L["h"] += x
            L["c"] += x * px
            if init and k not in d["init"]:
                d["init"].append(k)

    def apply_trade(self, t, key):
        fee_usd = super().apply_trade(t, key)
        d, cfg = self.store.data, self.cfg
        pk = parse_key(key)
        if not pk or not 0 <= pk[0] < cfg.n:
            log.warning("fill ของ key ผิดรูป %s", key)
            return fee_usd
        k, _ = pk
        amt, px = float(t["amount"]), float(t["price"])
        fee, fc = float(t.get("fee") or 0), (t.get("fee_currency") or cfg.quote).upper()
        fee_btc = fee if fc == cfg.base else 0.0
        L = self._layer(k)
        if t["direction"] == "buy":
            L["h"] += amt - fee_btc
            L["c"] += amt * px + (fee if fc == cfg.quote else 0.0)
        else:
            out = amt + fee_btc
            frac = min(1.0, out / L["h"]) if L["h"] > 0 else 1.0
            basis = L["c"] * frac
            L["h"] -= out
            L["c"] -= basis
            pnl = amt * px - (fee if fc == cfg.quote else 0.0) - basis
            done = L["h"] * cfg.sell_price(k) < cfg.min_order_usd           # ขายหมดชั้น (เหลือแค่เศษ)
            t["_init"] = k in d["init"]
            if k in d["init"]:
                d["init_pnl"] += pnl
                if done:
                    d["init"].remove(k)
                    d["cap"].pop(str(k), None)
            else:
                d["grid_pnl"] += pnl
                d["rounds"] += done
            t["_pnl"] = pnl
        if abs(L["h"]) < 1e-12:
            L["h"] = 0.0
        return fee_usd

    def hodl_diff(self, price):
        """กำไร/ขาดทุนรวม = มูลค่าพอร์ตตอนนี้ − เงินที่ใส่ (ยอดตอนเริ่ม + โอนเข้า − โอนออก)"""
        s, e = self.store.data.get("start") or {}, self.store.data["exp"]
        if not s:
            return None
        return e["base"] * price + e["quote"] - (s.get("base_usd", 0.0) + s.get("quote", 0.0))

    def fill_text(self, t, fee_usd):
        amt, px = float(t["amount"]), float(t["price"])
        d, e, cfg = self.store.data, self.store.data["exp"], self.cfg
        k, _ = parse_key(self.is_own(t.get("label"), t.get("order_id")) or "") or (-1, "")
        head = (f"{'🔴 ขาย' if t['direction'] == 'sell' else '🟢 ซื้อ'} ชั้น {k} ({cfg.buy_price(k):,.0f}→{cfg.sell_price(k):,.0f}) "
                f"{amt:.8f} {cfg.base} @ {px:,.2f} = ${amt * px:,.2f} · fee ${fee_usd:,.4f}")
        if "_pnl" in t:
            head += f"\nกำไรไม้นี้ {t['_pnl']:+,.2f}$ ({'ชั้นตั้งต้น = capital gain' if t.get('_init') else 'รอบกริด'})"
        pnl = self.hodl_diff(px)
        return (head + f"\n{cfg.base} {e['base']:.8f} (${e['base'] * px:,.2f}) · {cfg.quote} {e['quote']:,.2f} · รวม ${e['base'] * px + e['quote']:,.2f}"
                + f"\nกำไรกริดแท้ {d['grid_pnl']:+,.2f}$ ({d['rounds']} รอบ) · ขายชั้นตั้งต้น {d['init_pnl']:+,.2f}$"
                + (f" · รวมทั้งพอร์ต {pnl:+,.2f}$" if pnl is not None else ""))

    def status_text(self):
        d, hb, cfg = self.store.data, self.store.data.get("heartbeat") or {}, self.cfg
        st = {"active": "✅ ทำงานอยู่", "halted": "🛑 หยุด (halt)", "closed": "✅ ปิดแล้ว", "new": "⏳ ยังไม่เริ่ม"}.get(d["status"], d["status"])
        lines = [f"{st} · {'LIVE' if self.live else 'DRY-RUN'} · {self.title()}"]
        if d.get("halt_reason"):
            lines.append(f"เหตุผล: {d['halt_reason']}")
        if hb:
            lines.append(f"ตรวจล่าสุด {self.now() - hb['ts']:,.0f} วิที่แล้ว · ราคา {hb['mid']:,.2f}")
            lines.append(f"ถือ {hb['layers_held']}/{cfg.n} ชั้น · {cfg.base} ${hb['value']:,.2f} ({hb['share'] * 100:.0f}%) · {cfg.quote} {hb['quote']:,.2f}")
            lines.append(f"ซื้อถัดไป {hb['next_buy'] or 0:,.0f} · ขายถัดไป {hb['next_sell'] or 0:,.0f} · ออเดอร์ของบอท {hb['own']}"
                         + (f" · ไล่ราคา ซื้อ {hb['chase_buy']} ขาย {hb['chase_sell']} ชั้น" if hb['chase_buy'] or hb['chase_sell'] else ""))
            if "surplus" in hb:
                lines.append(f"เงินสดส่วนเกิน (นอกกริด ถอนได้) {hb['surplus']:+,.2f} · กันไว้ซื้อถึง floor {hb['reserve']:,.2f}")
            lines.append(f"กำไรกริดแท้ {d['grid_pnl']:+,.2f}$ ({d['rounds']} รอบ) · ขายชั้นตั้งต้น {d['init_pnl']:+,.2f}$"
                         + (f" · รวมทั้งพอร์ต {hb['hodl']:+,.2f}$" if hb.get("hodl") is not None else "") + f" · fee ${d.get('fees_usd', 0):,.2f}")
        lines.append(f"รันมา {(self.now() - self.started_at) / 3600:,.1f} ชม. · fill ทั้งหมด {len(d['fills'])} ครั้ง")
        return "\n".join(lines)

    # ---------- เริ่ม ----------
    async def startup(self):
        r = await self.stable_read()
        if r is None:
            raise Refuse("ข้อมูลไม่นิ่งตอนเริ่ม ลองใหม่")
        if not (r["bid"] > 0 and r["ask"] > r["bid"]):
            raise Refuse(f"order book ของ {self.cfg.instrument} ไม่ปกติ (bid {r['bid']} / ask {r['ask']})")
        foreign = [o for o in r["orders"] if not self.is_own(o.label, o.order_id)]
        if foreign:
            raise Refuse(f"มีออเดอร์ {self.cfg.instrument} ที่ไม่ใช่ของบอท {len(foreign)} ตัว — ใช้ subaccount แยกสำหรับบอทนี้ หรือยกเลิกออเดอร์เหล่านั้นก่อน")
        d, cfg, notes = self.store.data, self.cfg, []
        mid = (r["bid"] + r["ask"]) / 2
        if d["status"] == "new":
            cap = start_cap(cfg, mid)
            try:
                add = allocate(cfg, {}, r["base"], mid, cap)
            except ValueError as e:
                raise Refuse(f"{e} — BTC ใน sub ({r['base']:.8f}) มากกว่ากริดทั้งหมดรับได้: โอนออก หรือเพิ่มทุน/ขยายกรอบ")
            if r["orders"]:
                raise Refuse("มีออเดอร์ของบอทค้างอยู่แต่ไฟล์สถานะเป็นบอทใหม่ — ยกเลิกออเดอร์ kz_ เหล่านั้นก่อน")
            d["layers"], d["init"], d["cap"] = {}, [], {str(k): v for k, v in cap.items()}
            self.add_btc(add, mid, init=True)
            for k in ideal_held(cfg, mid):                         # ชั้นที่ ratio ควรถือ = ชั้นตั้งต้น (ซื้อตอนเริ่ม)
                if k not in d["init"]:
                    d["init"].append(k)
            sp = start_plan(cfg, self.h(), r["ask"], cap)
            if (sp["buy_usd"] >= cfg.cost or sp["sell_btc"] * r["bid"] >= cfg.cost) and not self.sf.rebalance_now:
                d["layers"], d["init"], d["cap"] = {}, [], {}
                raise Refuse(f"เริ่มแบบ ratio ที่ราคา {mid:,.0f}: ต้องถือ {sp['layers_ideal']} ชั้น → "
                             + (f"ซื้อทันที ~${sp['buy_usd']:,.2f} ({sp['buy_btc']:.8f} {cfg.base}) " if sp["buy_usd"] else "")
                             + (f"ขายทันที ~${sp['sell_usd']:,.2f} " if sp["sell_btc"] else "")
                             + "ด้วย post-only ทยอยทีละชั้น — ยืนยันด้วย --rebalance-now")
            if sp["buy_usd"] * (1 + cfg.fee_rate) > r["quote"]:
                notes.append(f"⚠️ {cfg.quote} {r['quote']:,.2f} ไม่พอซื้อชั้นตั้งต้นครบ (ต้องการ ~{sp['buy_usd']:,.2f})")
            d["exp"] = {"base": r["base"], "quote": r["quote"]}
            d["start"] = {"ts": int(self.now() * 1000), "mid": mid, "base": r["base"], "quote": r["quote"], "base_usd": r["base"] * mid}
            d["ledger_start_ms"] = int(self.now() * 1000)
            notes.append(f"เริ่มนับ: {r['base']:.8f} {cfg.base} + {r['quote']:,.2f} {cfg.quote} · ชั้นตั้งต้น {len(d['init'])} ชั้น"
                         + (f" · ต้องซื้อ ~${sp['buy_usd']:,.2f}" if sp["buy_usd"] else ""))
        else:
            if d["status"] == "closed":
                raise Refuse("บอทนี้ปิดไปแล้ว")
            new, foreign_t = await self.sync_trades()
            for t, fee, text in new:
                notes.append("ระหว่างบอทหยุด: " + text.split("\n")[0])
                self.log_fill(t, fee)
            if foreign_t:
                raise Refuse(f"มีการเทรด {cfg.instrument} ที่ไม่ใช่ของบอท {len(foreign_t)} รายการระหว่างบอทหยุด — ตรวจบัญชีแล้วเริ่มด้วย --accept-balance")
            db = r["base"] - d["exp"]["base"]
            if abs(db) > 1e-7:
                if not self.sf.accept_balance:
                    raise Refuse(f"ยอด {cfg.base} ในบัญชี {r['base']:.8f} ≠ บัญชีไม้ {d['exp']['base']:.8f} (ต่าง {db:+.8f}) "
                                 "— มีการโอนเข้า/ออก? ตรวจแล้วเริ่มด้วย --accept-balance")
                try:
                    if db > 0:
                        self.add_btc(allocate(cfg, self.h(), db, mid, self.cap()), mid, init=True)
                    else:
                        for k, x in deallocate(cfg, self.h(), -db, mid).items():
                            L = self._layer(k)
                            L["c"] -= L["c"] * x / L["h"] if L["h"] > 0 else 0.0
                            L["h"] -= x
                except ValueError as e:
                    raise Refuse(str(e))
                d["start"]["base_usd"] = d["start"].get("base_usd", 0.0) + db * mid     # เงินโอน ไม่นับเป็นกำไร
                d["exp"]["base"] = r["base"]
                notes.append(f"รับยอด {cfg.base} ใหม่ ({db:+.8f}) เข้าชั้น")
            dq = r["quote"] - d["exp"]["quote"]
            if abs(dq) > 0.01:
                d["start"]["quote"] += dq
                d["exp"]["quote"] = r["quote"]
                notes.append(f"{cfg.quote} เปลี่ยนนอกบอท {dq:+,.2f}")
        need = cash_to_floor(cfg, self.h(), self.cap(), mid)
        if need > r["quote"] + 0.01:
            notes.append(f"ℹ️ ถ้าราคาลงถึง floor {cfg.floor:,.0f} ต้องใช้ {cfg.quote} ~{need:,.2f} (มี {r['quote']:,.2f}) — ไม่พอ ฝั่งซื้อจะหยุดก่อนถึง floor")
        d["status"] = "active"
        d["halt_reason"] = ""
        if self.live:
            self.store.save()
        return r, notes

    # ---------- ตัดสินใจ ----------
    async def decide(self, r, now):
        d, cfg = self.store.data, self.cfg
        h = self.h()
        if abs(sum(h.values()) - d["exp"]["base"]) > 1e-7:
            return await self.halt(f"ผลรวมบัญชีไม้รายชั้น {sum(h.values()):.8f} ≠ ยอด {cfg.base} {d['exp']['base']:.8f} (บั๊ก?)")
        own = [(self.is_own(o.label, o.order_id), o) for o in r["orders"] if self.is_own(o.label, o.order_id)]
        dec = plan(cfg, h, d["exp"]["quote"], r["bid"], r["ask"], own, pending=set(self.pending), band=r["band"], cap=self.cap())
        self.last = dec
        i = dec.info
        if i.get("usdc_short"):
            self.n.post(f"⚠️ {cfg.quote} ไม่พอซื้อชั้นถัดไป (ต้องการ ~{i['usdc_short']:,.2f} ว่าง {max(i['free_cash'], 0):,.2f}) — ข้ามฝั่งซื้อ ฝั่งขายทำงานต่อ", key="short")
        eq = d["exp"]["base"] * i["mid"] + d["exp"]["quote"]
        reserve = cash_to_floor(cfg, h, self.cap(), i["mid"])                  # USDC ที่ต้องกันไว้ซื้อทุกชั้นที่ยังไม่เต็มจนถึง floor
        d["heartbeat"] = {"surplus": d["exp"]["quote"] - reserve, "reserve": reserve,"ts": now, "mid": i["mid"], "value": i["value"], "quote": d["exp"]["quote"], "share": i["value"] / eq if eq > 0 else 0.0,
                          "layers_held": i["layers_held"], "next_buy": i["next_buy"], "next_sell": i["next_sell"], "chase_buy": i["chase_buy"],
                          "chase_sell": i["chase_sell"], "waiting": len(i["waiting_band"]), "own": len(own), "hodl": self.hodl_diff(i["mid"]),
                          "grid_pnl": d["grid_pnl"], "init_pnl": d["init_pnl"], "rounds": d["rounds"], "live": self.live}
        if now - self.last_hb_log >= self.sf.heartbeat_sec:
            self.last_hb_log = now
            log.info("%s", f"✓ ทำงานปกติ · ราคา {i['mid']:,.2f} · ถือ {i['layers_held']}/{cfg.n} ชั้น {cfg.base} ${i['value']:,.2f} · "
                           f"{cfg.quote} {d['exp']['quote']:,.2f} · ซื้อ {i['next_buy'] or 0:,.0f} / ขาย {i['next_sell'] or 0:,.0f} · ออเดอร์ {len(own)}"
                           + (f" · ไล่ราคา {i['chase_buy']}/{i['chase_sell']}" if i['chase_buy'] or i['chase_sell'] else ""))
        await self.execute(dec.actions)
        if self.live:
            self.store.save()
