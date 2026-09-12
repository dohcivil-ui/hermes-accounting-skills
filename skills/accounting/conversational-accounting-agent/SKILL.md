---
name: conversational-accounting-agent
description: "Use for natural-language, read-only accounting questions and conversational follow-ups grounded in Google Sheets."
version: 1.0.0
author: Lekza (Hermes Agent)
platforms: [linux]
metadata:
  hermes:
    category: accounting
    tags: [telegram, accounting, conversational, reporting, read-only]
---

# conversational-accounting-agent

ใช้เมื่อผู้ใช้ถามข้อมูลบัญชีด้วยภาษาธรรมชาติ เช่น:

- "วันนี้จ่ายเท่าไหร่"
- "สัปดาห์นี้เงินเข้าเท่าไหร่"
- "เดือนนี้มีทั้งหมดกี่รายการ"
- "ตอนนี้มีกี่โครงการ"
- "ของสนามบินล่ะ"
- "ร้านเหล็กได้รับเงินไปเท่าไหร่"
- "ทำไมยอดงานนี้เยอะ"

## หน้าที่ของ Hermes / Gemini

Hermes หรือ Gemini เป็น conversational orchestration layer:

1. อ่านคำถามและบทสนทนาก่อนหน้าเป็นภาษาธรรมชาติ ห้าม route ด้วยรายการคำหรือ regex แบบตายตัว
2. แปลงคำถามเป็น structured request สำหรับ query module เท่านั้น
3. สำหรับ follow-up ให้ใช้ `context` จากผลครั้งก่อนเป็น `previous_context` แล้วใส่เฉพาะค่าที่ผู้ใช้เปลี่ยน
4. เรียบเรียงคำตอบให้อ่านง่าย แต่ใช้ตัวเลขจากผล query เท่านั้น ห้ามคำนวณหรือเดาตัวเลขเอง
5. หากผลระบุว่าโครงการหรือชื่อบุคคลกำกวม ให้ถามผู้ใช้เลือก ห้ามเดา

Structured intents ที่รองรับ:

- `totals`: ยอดเงินเข้า เงินออก สุทธิ และจำนวนรายการ
- `project_count`: จำนวนโครงการทั้งหมดและโครงการที่ active
- `party_summary`: สรุปคนรับเงิน (`payee`), คนโอนเงิน (`payer`) หรือทั้งสองแบบ
- `follow_up`: ใช้ intent/ช่วงเวลา/metric จาก `previous_context` แล้วเปลี่ยน filter ตามคำถามล่าสุด
- `explain`: แจกแจง deterministic drivers จาก context เดิม เช่น หมวด คนรับเงิน/คนโอน และรายการยอดสูง

ช่วงเวลา: `today`, `week` (7 วันรวมวันนี้), `month`, `all`

metric: `expense`, `income`, `net`, `all`

## กติกากลางสำหรับคำถามบัญชี

- ใช้ช่วงเวลาที่ผู้ใช้ระบุ; คำถามใหม่ที่ไม่ระบุช่วงเวลาใช้ `today` และไม่ส่ง `previous_context` ของเรื่องก่อน ถ้าขอข้อมูลตลอดช่วงเวลาตั้งแต่เริ่มใช้ `all` ("เดือนนี้มีทั้งหมดกี่รายการ" ยังใช้ `month`)
- คำถามต่อเนื่องใช้ช่วงเวลาและ filter จาก `context` เดิม เปลี่ยนเฉพาะค่าที่ผู้ใช้ระบุ และแสดงช่วงเวลาจาก `grounding` ทุกครั้งที่ตอบยอดเงิน
- ระบุ `party_role` และ `metric` ตามความหมาย: "จ่ายให้ร้าน" ใช้ `payee` + `expense`, "รับเงินจากลูกค้า" ใช้ `payer` + `income`; ถ้าถามทั้งสองบทบาทใช้ `both` และแสดงแยก ห้ามนำยอดสองบทบาทมาบวกเอง
- `query-payee-summary` ใช้ query module และกติกานี้ร่วมกัน ไม่มีการอ่าน/รวมยอดอีกเส้นทาง
- `party_summary` คืนยอดและจำนวนแยกบุคคล/บทบาท ไม่คืนรายละเอียดทุกรายการหรือยอดบุคคลแยกทุกโครงการ; แสดงเฉพาะข้อมูลที่ module คืนมา

## Query module

เรียกเฉพาะ read-only module นี้:

```text
python3 /data/skills/accounting/conversational-accounting-agent/scripts/query_accounting.py --request-json '<JSON>'
```

ตัวอย่าง request แรก:

```json
{"intent":"totals","period":"today","metric":"expense"}
```

ตัวอย่าง follow-up "ของสนามบินล่ะ":

```json
{"intent":"follow_up","project":"สนามบิน","previous_context":{"intent":"totals","period":"today","metric":"expense","project":null,"party":null,"party_role":"both"}}
```

ตัวอย่าง follow-up "ทำไมเยอะ":

```json
{"intent":"explain","previous_context":{"intent":"totals","period":"today","metric":"expense","project":"สนามบิน","party":null,"party_role":"both"}}
```

Module นี้ reuse `ReportingSheetsReader`, อ่าน frozen `Projects` และ `Transactions` schemas, นับเฉพาะ `status = confirmed`, ใช้เวลา `Asia/Bangkok` และคำนวณด้วย `Decimal`

## กฎความปลอดภัย

- เป็น read-only เท่านั้น ไม่มี interface สำหรับ create/update/delete/confirm
- ห้ามใช้ตัวเลขจากความจำ การประมาณ หรือคำตอบเดิมแทนผล query ล่าสุด
- ห้ามให้ AI รวมยอดเอง ให้ใช้ `result` และ `grounding` จาก module เท่านั้น
- คำสั่งเพิ่ม แก้ ลบ ย้าย หรือยืนยัน transaction ต้องกลับไปใช้ durable transaction flow และ confirmation เดิม
- ห้ามเรียก Google Sheets writer, Drive writer หรือ transaction mutation จาก skill นี้
- ถ้า query ล้มเหลว ให้แจ้งว่าอ่านข้อมูลไม่ได้ ห้ามสร้างคำตอบตัวเลขทดแทน
