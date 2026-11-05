# RBSI Grid Bot — วางกริด Rebalance Short Inverse บน Deribit

บอทดูแล **zone ย่อยหนึ่ง zone** ของระบบ Rebalance Short Inverse บน `BTC-PERPETUAL`
คุณกำหนด กรอบล่าง / กรอบบน / gap / ขนาดต่อชั้น — บอทวางและดูแลออเดอร์ให้ครบตามสูตรตลอดเวลา
**การต่อพอร์ต (handoff ไป sub ถัดไป) ทำเองนอกบอท**

- ทุกคำสั่งเป็น **limit post-only** (จ่าย fee แบบ maker เท่านั้น ไม่มี market order)
- เริ่มต้นเป็น **dry-run** เสมอ — ส่งคำสั่งจริงต้องมีทั้ง `live_allowed = true` ใน config และ `--live`
- แจ้งเตือนทาง Telegram

## โมเดลของ zone

ตัวอย่าง `lower 82,000 · upper 88,000 · gap 500 · size 1,000` → 12 ชั้น เพดาน short 12,000 USD

| | ราคา sell | ราคา buy คืน |
|---|---|---|
| ชั้น k | lower + k×gap | lower + (k−1)×gap |

- ชั้นที่ short อยู่ มี **buy reduce-only** รอที่ราคา buy · ชั้นที่ว่าง มี **sell** รอที่ราคา sell
- position = −(ผลรวมยอด short ของทุกชั้น) · ราคาชนกรอบล่าง → position ต้องเป็น 0 · ชนกรอบบน → เต็ม 12,000
- ชั้นที่ fill บางส่วน มีทั้ง sell (ส่วนที่ยังไม่ขาย) และ buy คืน (ส่วนที่ขายไปแล้ว) พร้อมกัน

## ด่านกัน double position ("ซ้ำที่ zone เดิม")

ตัวอย่างอันตราย: sell 84,000 fill แล้ว ออเดอร์หายจากรายการ → บอทเข้าใจผิดว่าหาย → วาง sell 84,000 ซ้ำ

1. **ชุดออเดอร์คำนวณจากยอด short รายชั้น ไม่ใช่จากออเดอร์ที่หายไป** — sell 84,000 fill แล้ว ชั้นนั้นกลายเป็นชั้น short ต้องมี buy 83,500 ไม่ใช่ sell 84,000
2. **ตรวจคำตอบ 3 ทาง**: position จาก exchange = บัญชีไม้ (ledger จาก fill ที่ติด label ของบอท) = ยอด fill ของออเดอร์ที่เปิดอยู่ — ไม่ตรง = รอ ไม่ลงมือ
3. **ออเดอร์ของบอทที่หายไป** ต้องถาม Deribit จนรู้ผลแน่ (filled/cancelled) และเห็นยอดใน ledger แล้ว ชั้นนั้นจึงวางใหม่ได้
4. **อ่านสถานะ 2 รอบ** ห่างกัน ต้องเหมือนกัน · ชั้น/ฝั่งที่เพิ่งส่งคำสั่งถูกล็อกจนยืนยันได้ (กันส่งซ้ำตอนคำตอบหาย)
5. **เพดานความเสี่ยง**: short + sell ที่รอ ≤ เพดาน zone เสมอ — เกิน = หยุดทันที
6. ทุกออเดอร์ติด label `rbsi_<zone>_<ชั้น>` · มี lock กันรันบอทซ้อนต่อ zone และต่อ API key

## ราคาวิ่งทะลุชั้น (post-only)

ถ้าราคาวิ่งผ่านชั้นก่อนบอทวางออเดอร์ฝั่งตรงข้ามทัน บอทวาง post-only ที่ **bid (ฝั่ง buy) / ask (ฝั่ง sell) ดีที่สุด** แทน
(ได้ราคาดีกว่าราคาชั้น) และเลื่อนตามเมื่อ bid/ask หนีเกิน `chase_frac × gap` · ราคากลับมา = กลับไปวางที่ราคาชั้น
ข้อแลก: ตลาดวิ่งแรงทางเดียว position อาจตามสูตรไม่ทันชั่วคราว · ตามไม่ทันเกิน `max_catchup` รายการพร้อมกัน = หยุด

## Traffic หนัก / ส่งคำสั่งไม่ไปหรือไปไม่ครบ

- ออเดอร์ของทุกชั้นวางค้างบน exchange ล่วงหน้า — ต่อให้บอทส่งอะไรไม่ได้ การ fill ที่ราคาชั้นยังเกิดบน Deribit ตามปกติ
  สิ่งที่ช้าไปมีแค่ออเดอร์ฝั่งตรงข้ามหลัง fill
- ส่งแล้วคำตอบหาย (timeout) → ล็อกชั้นไว้ `pending_ttl` วิ แล้วตัดสินจากสถานะจริงบน exchange ไม่ส่งซ้ำ
- ส่งไม่ครบ → ทุกรอบคำนวณชุดที่ควรมีใหม่ ตัวที่ขาดถูกส่งรอบถัดไปเอง
- จัดคิวตามความเร่งด่วน (ยกเลิก → ใกล้ราคาที่สุดก่อน) · ส่งไม่เกิน `max_orders_per_cycle` ต่อรอบ เว้น `order_spacing_sec`
- Deribit จำกัดความถี่ (10028 ฯลฯ) → หยุดชุดนั้น พักแบบทวีคูณถึง `throttle_max_sec` แล้วส่งต่อ
- สายหลุด → ต่อใหม่พร้อม backoff แล้วสร้างสถานะจาก exchange ใหม่ทั้งหมด
- ข้อแนะนำเพิ่ม: ปิด **Cancel on Disconnect** ของบัญชี (ให้ออเดอร์ค้างไว้ตอนบอทหลุด) · รันบนเครื่องที่ไม่ sleep (`caffeinate -i`) และเน็ตเสถียร

## หน้าเว็บควบคุม

ดับเบิลคลิก `rbsi-ui.command` → http://127.0.0.1:8777 (ครั้งแรกติดตั้ง .venv + library ให้เอง ต้องมี Python 3.11 ขึ้นไป)
ทำได้ทุกอย่างจากหน้าเว็บ: API key หลาย sub (เพิ่ม/ลบ) · Telegram (token / chat id / ทดสอบ) · ตั้งค่า-ตรวจ-เริ่ม (live หรือ dry-run)-หยุด-ปิด ทั้งบอทกริดและบอท Fixed Value
ตั้ง zone ในฟอร์ม · ดึงบัญชีจาก key ใน `.env` ให้เอง · ตรวจ / เริ่ม LIVE / หยุด / ปิด zone ได้จากปุ่ม
เปิดได้เฉพาะเครื่องนี้ · ปิดหน้าเว็บแล้วบอทยังรันต่อ

## บอทตัวที่ 2: Fixed Asset Value (Deribit spot)

ถือ BTC ให้มีมูลค่าคงที่ (เช่น $3,500) ใน subaccount แยก · มูลค่าห่างเป้าถึง Δ → ขาย/ซื้อส่วนต่างกลับเข้าเป้าด้วย post-only
ไฟล์ตั้งค่าอยู่ใน `fv/` (ดู `fv/example-fixed-value.toml`) · ใช้คำสั่งเดิม: `plan` / `check` / `run` / `status` / `close` กับ `-c fv/<ไฟล์>.toml`

```bash
.venv/bin/python -m rbsi_bot plan  -c fv/example-fixed-value.toml --price 84000 --usdc 3600   # ออฟไลน์: ราคาขาย/ซื้อ บันได และ USDC หมดที่ราคาไหน
.venv/bin/python -m rbsi_bot check -c fv/<ไฟล์>.toml                                          # อ่านบัญชีจริง ไม่ส่งคำสั่ง
```

## ติดตั้ง

```bash
cd ~/rbsi-grid-bot
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env        # ใส่ key + Telegram
cp config.example.toml zones/beta2-82k-88k.toml
```

- **API key**: สร้างใน subaccount ที่ให้บอทเทรด สิทธิ์ `trade:read_write` (ไม่ต้องมี wallet/withdraw) ล็อก IP ถ้าทำได้ — แยกจาก key read-only ของ position monitor
- **Telegram**: @BotFather → token · ทักบอทหนึ่งครั้ง แล้วเปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` หา `chat.id`

## ตั้งค่า zone ด้วยหน้าเว็บ

```bash
open ~/rbsi-grid-bot/zone-setup.html
```

กรอกกรอบ/gap/ขนาด (+ position/equity ตอนนี้ถ้าอยากให้ตรวจ) → หน้าเว็บตรวจกติกาเดียวกับบอท แสดงออเดอร์ที่คาดหวัง ณ ราคาสดของ Deribit
ประมาณ liq ตอน zone เต็ม (สูตรเดียวกับบอท) → กด **"คัดลอกคำสั่งสร้างไฟล์"** แล้ววางใน terminal จะได้ไฟล์ใน `zones/` พร้อมคำสั่ง CLI ด้านล่าง

## ขั้นตอนใช้งาน

```bash
B=".venv/bin/python -m rbsi_bot"
$B plan  -c zones/beta2-82k-88k.toml --price 83458      # ดูชุดออเดอร์ (ออฟไลน์)
$B check -c zones/beta2-82k-88k.toml --adopt-manual      # ต่อจริง อ่านสถานะ บอกว่าจะทำอะไร (ไม่ส่งคำสั่ง)
$B run   -c zones/beta2-82k-88k.toml --adopt-manual      # dry-run: log ว่าจะทำอะไร
$B run   -c zones/beta2-82k-88k.toml --adopt-manual --live   # จริง (ต้อง live_allowed = true)
$B status -c zones/beta2-82k-88k.toml
$B close -c zones/beta2-82k-88k.toml --live              # ยกเลิกออเดอร์ของบอท + ปิด zone ถาวร
```

**แนะนำให้เริ่มด้วย zone เล็ก** (เช่น 2–3 ชั้น ขนาด 10–100 USD) ดูการทำงานสักพักก่อนใช้กับ zone จริง

### รับช่วงครั้งแรก (`--adopt-manual`)
อ่าน position ต้องเป็น short ทวีคูณของ size และสอดคล้องกับราคา → ตั้ง baseline (ชั้น 1..m ถือว่า short) →
**ออเดอร์มือที่ตรงสูตร (ฝั่ง/ราคา/ขนาด ยังไม่ fill) นับเป็นของบอทเลย** โดยผูก order id กับชั้น ไม่ยกเลิกไม่วางใหม่ ·
ชั้นที่ไม่มีออเดอร์ (เช่นคุณถอดออก) บอทวางเพิ่มพร้อม label · ออเดอร์มือที่ไม่ตรงสูตรถูกยกเลิก · ถ้าไม่เข้าเงื่อนไข บอทไม่เริ่ม
ด่านก่อนเริ่ม: liq ถ้า zone เต็ม (สูตรเดียวกับ position monitor สอบเทียบ MM จาก position จริง) ต้องสูงกว่า upper + `liq_buffer`
หลังรับช่วงแล้ว **อย่าวาง/ยกเลิกออเดอร์บน BTC-PERPETUAL ใน subaccount นี้เอง** — บอทจะเห็น fill ที่ไม่ใช่ของมันแล้วหยุด

### เมื่อบอทหยุด (HALT)
ยกเลิก sell ของบอท (ออเดอร์ที่เพิ่มความเสี่ยง) เก็บ buy reduce-only ไว้ แล้วแจ้ง Telegram
สาเหตุที่หยุด: มีออเดอร์/fill ที่ไม่ใช่ของบอท (เทรดมือหรือโดน liquidate) · position เป็น long หรือเกินเพดาน · ความเสี่ยงรวมเกินเพดาน ·
ข้อมูลไม่นิ่งนานเกิน `flux_halt_sec` · ราคาทิ้งห่างเกิน `max_catchup` · ตรวจแล้วรันใหม่ด้วย `--clear-halt`

### ต่อพอร์ต
เมื่อ zone เต็ม บอทแจ้งเตือนและหยุดเพิ่ม short (คง buy ไว้) · ถ้าคุณปิด position เองหลัง zone เต็ม บอทถือว่าต่อพอร์ตแล้ว
ยกเลิกออเดอร์ที่เหลือ และปิด zone **ถาวร** (รัน zone เดิมซ้ำไม่ได้) · หรือสั่ง `close` เอง

## ทดสอบ

```bash
.venv/bin/python -m pytest -q          # 53 เทสต์: ตรรกะกริด + สูตร liq (เทียบ JS ของ monitor) + จำลองทั้งระบบ
.venv/bin/python -m tests.soak 200     # ทดสอบหนัก 5 สภาพ × 200 เส้นทางราคา
```

exchange จำลองจงใจให้ข้อมูลไม่พร้อมกัน (position/fill มาช้า ออเดอร์หายทันที), partial fill, คำตอบหาย, ราคากระโดด,
rate limit — ตรวจทุกก้าวว่าไม่เคย long, ไม่เกินเพดาน, ไม่มี double, ไม่มี market order

## โครงไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `rbsi_bot/grid.py` | ตรรกะกริดล้วน (ทดสอบได้ทั้งหมด): ชุดออเดอร์ที่ควรมี + ตรวจคำตอบ |
| `rbsi_bot/bot.py` | วงรอบหลัก: อ่าน 2 รอบ → ledger → ตามหาออเดอร์ที่หาย → ตัดสินใจ → ส่งคำสั่ง |
| `rbsi_bot/deribit.py` | WebSocket JSON-RPC (โครงเดียวกับ deribit-monitor) |
| `rbsi_bot/liq.py` | สูตร liq (port จาก deribit-monitor) |
| `rbsi_bot/store.py` | ไฟล์สถานะ zone + ledger + lock (`~/.rbsi-bot/`) |
| `rbsi_bot/notify.py` | Telegram |

## ข้อจำกัด

- label ของ Deribit ใช้ `a-z 0-9 - _` · ยังไม่ได้ทดสอบกับบัญชีจริง — ให้ลองกับ zone เล็กก่อน
- ค่า MM default 1.26% มาจากจอจริง 26 ก.ย. 2026 (ปัด 4 ตำแหน่ง) · เมื่อมี position บอทสอบเทียบจากค่าจริงเอง
- บอทไม่รู้เรื่อง zone อื่น/sub อื่น — หนึ่ง process ต่อหนึ่ง zone ต่อหนึ่ง subaccount
