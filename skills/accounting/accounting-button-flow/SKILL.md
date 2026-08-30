---
name: accounting-button-flow
description: ปุ่มโต้ตอบ Telegram สำหรับระบบบัญชี Lekza หลัง AksonOCR อ่านสลิปแล้ว ใช้ clarify เพื่อเลือก Projects, Users, ประเภท, หมวด, แก้ไข และ Confirm โดยลดการพิมพ์เอง
version: 1.0.0
metadata:
  hermes:
    category: accounting
    tags: [telegram, buttons, accounting, projects, users, transactions]
---

# accounting-button-flow

## เป้าหมาย
หลังได้รับ `[AksonOCR Slip Result]` ให้ใช้ปุ่ม Telegram ผ่าน `clarify` เป็นหลัก
ห้ามให้ผู้ใช้พิมพ์เมื่อมีตัวเลือกที่เลือกได้จากปุ่ม

## กฎหลัก
- ใช้ AksonOCR result เดิม ห้าม OCR ซ้ำระหว่าง wizard
- เก็บ pending state ตาม `session_id + telegram user_id + reference_no`
- ก่อน Confirm ห้ามเขียน Drive / Projects / Users / Transactions
- `✏️ บันทึกเอง` ใช้สำหรับกรอกข้อความเองเมื่อข้อมูล/ตัวเลือกไม่ตรง
- `❌ ยกเลิก` ต้องล้าง pending state ของรายการนั้น
- ถ้า `reference_no` มีใน Transactions แล้ว ห้ามบันทึกซ้ำ

## Wizard หลัง OCR

### 1. สรุป OCR
แสดงสั้น ๆ:
- วันที่/เวลา
- จำนวนเงิน
- ผู้โอน
- ผู้รับ
- reference_no
- note
- confidence

### 2. Projects
อ่าน Projects ก่อน แล้วเรียก `clarify`:
คำถาม: `เลือกโครงการสำหรับรายการนี้`
choices:
- รายชื่อโครงการที่ active (ให้เรียง frequent/recent ก่อน)
- `➕ สร้างโครงการใหม่`
- `❌ ยกเลิก`

ถ้าเลือก `➕ สร้างโครงการใหม่`:
- ใช้ open-ended `clarify` ถามชื่อโครงการ
- ถาม customer เฉพาะเมื่อจำเป็น
- pending `new_project=true`
- ยังไม่เขียน Projects จน Confirm

ถ้ากด `✏️ บันทึกเอง`:
- ใช้ข้อความที่ผู้ใช้พิมพ์เป็น project name ชั่วคราว

### 3. Users
อ่าน Users ด้วย Telegram `user_id`.

ถ้าพบ user เดิม:
เรียก `clarify`:
คำถาม: `ผู้ส่งรายการ`
choices:
- `✅ ใช้ <name>`
- `👤 เลือกผู้ใช้อื่น`
- `➕ เพิ่มผู้ใช้ใหม่`
- `❌ ยกเลิก`

ถ้ายังไม่พบ:
choices:
- `➕ สร้างจาก Telegram`
- `➕ เพิ่มผู้ใช้ใหม่`
- `❌ ยกเลิก`

ผู้ใช้ใหม่เก็บ pending ก่อน และเขียน Users หลัง Confirm เท่านั้น.

### 4. ประเภทรายการ
เรียก `clarify`:
คำถาม: `เลือกรายการ`
choices:
- `🟢 รายรับ`
- `🔴 รายจ่าย`
- `❌ ยกเลิก`

### 5. หมวด
ถ้าเป็นรายจ่าย:
choices:
- `🧱 ค่าวัสดุ`
- `👷 ค่าแรง`
- `🚚 ค่าเดินทาง/ขนส่ง`
- `🧾 ค่าผู้รับเหมา`
- `📦 อื่นๆ`

ถ้าเป็นรายรับ:
choices:
- `💰 รับงวดงาน`
- `💵 เงินทดรอง/คืนเงิน`
- `📦 อื่นๆ`

ถ้ากด `✏️ บันทึกเอง` ให้รับหมวดจากข้อความถัดไป.

### 6. ตรวจสอบ
แสดง:
- project
- submitted_by
- type
- category
- date
- amount เป็น `#,##0.00`
- payer
- payee
- reference_no
- note
- confidence

เรียก `clarify`:
คำถาม: `ข้อมูลถูกต้องและพร้อมบันทึกหรือไม่`
choices:
- `✅ คอนเฟิร์มบันทึก`
- `✏️ แก้ไข`
- `❌ ยกเลิก`

ถ้าเลือกแก้ไข ให้เรียก `clarify`:
choices:
- `โครงการ`
- `ผู้ส่ง`
- `ประเภท`
- `หมวด`
- `จำนวนเงิน`
- `ผู้โอน`
- `ผู้รับ`
- `หมายเหตุ`
จากนั้นกลับมาตรวจสอบอีกครั้ง.

## Save Order หลัง Confirm
ทำตามลำดับนี้เท่านั้น:

1. ตรวจ duplicate `reference_no`
2. สร้าง Projects/Users ใหม่เฉพาะ master data ที่ pending และยังไม่มี
3. Upload `source_image_path` ไป Google Drive parent จาก `LEKZA_SLIP_FOLDER_ID`
4. ต้องได้ `file_id` + `webViewLink`
5. Append Transactions 1 แถว และใส่ `slip_url = webViewLink`
6. `status = confirmed`
7. ลบ pending state หลังทุกขั้นสำเร็จ

ถ้า Drive ล้มเหลว:
- ห้าม append Transactions
- ห้าม status confirmed
- เก็บ pending ไว้ retry

ถ้า Sheets ล้มเหลวหลัง Drive สำเร็จ:
- เก็บ `drive_file_id`/`webViewLink` ใน pending
- retry Sheets โดยไม่ upload Drive ซ้ำ

## Sheet Roles
- Projects = master โครงการ
- Users = master ผู้ใช้
- Transactions = รายการรับ/จ่ายทุกครั้ง

ห้ามสร้าง Projects/Users ซ้ำทุก Transaction.
