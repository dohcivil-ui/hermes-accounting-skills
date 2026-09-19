---
name: accounting-button-flow
description: อธิบายปุ่มและการกรอกข้อมูลของ durable transaction flow หลัง OCR โดยให้ plugin จัดการ wizard และ Confirm
version: 1.0.0
metadata:
  hermes:
    category: accounting
    tags: [telegram, buttons, accounting, projects, users, transactions]
---

# accounting-button-flow

## เจ้าของ flow

`accounting-slip-bridge` ส่งผล AksonOCR เดิมเข้า `accounting-transaction-buttons`; `TransactionFlow` เก็บ state ใน SQLite และ controller แสดงปุ่มตาม state/version ของรายการ

- ให้ผู้ใช้เลือกจากปุ่มของรายการเดิม ห้าม Hermes สร้าง wizard หรือ Confirm/Cancel ซ้อนผ่าน `clarify`
- Controller รับข้อความกรอกเองตามรายการและ `entry_mode` ที่ durable flow เลือกไว้ ห้ามผูกข้อความเข้ารายการหรือเก็บ pending อีกชุดเอง
- ลำดับปกติ: ข้อมูล OCR ที่ขาด → โครงการ → ผู้ส่งรายการ → ประเภท → หมวด → ตรวจสอบ
- หน้าตรวจสอบมี Confirm / กลับ / ยกเลิก; ห้ามเสนอเมนูเลือกผู้ใช้อื่นหรือแก้ไขรายช่องที่ controller ยังไม่รองรับ
- Confirm / กลับ / ยกเลิก / ลองใหม่ เป็นหน้าที่ของ callback เดิม; ห้ามลบ state หรือถือว่าข้อความยืนยันทั่วไปแทน callback ได้

## การบันทึก

- Durable flow ตรวจผู้ใช้ version และข้อมูลก่อนยืนยัน; `ProductionSavePipeline` จัดการ Drive → Transactions พร้อม idempotency และ retry
- ห้าม Agent เรียก writer, upload, append, สร้าง Projects/Users หรือจัดการ retry เอง เส้นทาง save นี้ยังไม่มีขั้นตอนสร้าง master data
- ห้าม OCR ซ้ำระหว่าง flow หรืออ้างว่าบันทึกสำเร็จจากความจำ; ใช้ผลที่ controller ยืนยันเท่านั้น
- ถ้าได้รับ `[AksonOCR Slip Result]` พร้อม `handoff_failed` ให้แจ้งว่าส่งต่อเข้า flow ไม่สำเร็จ ไม่ทราบสถานะบันทึก และต้องตรวจรายการเดิมก่อนดำเนินการต่อ ห้ามเปิด wizard ขอ Confirm หรือสั่งส่งสลิปซ้ำ
- คำถามสรุปบัญชีใช้ [conversational-accounting-agent](../conversational-accounting-agent/SKILL.md); query นี้ยังไม่อ่าน pending จาก SQLite จึงใช้ยืนยันสถานะรายการค้างไม่ได้
