---
name: process-slip-pipeline
description: Use for Telegram slip processing through the AksonOCR bridge and durable transaction handoff.
platforms: [linux]
metadata:
  hermes:
    category: accounting
    tags: [telegram, accounting, slips, aksonocr]
---

# Process Slip Pipeline (AksonOCR-First)

## OCR และการส่งต่อ

- `accounting-slip-bridge` เป็นเจ้าของ OCR สำหรับภาพจาก Telegram: ตรวจ ingress/duplicate แล้วใช้ AksonOCR จาก environment ของ runtime ที่เลือก
- Bridge normalize ผลเดิมและส่งเข้า `accounting-transaction-buttons` เพื่อสร้าง/กู้คืน durable transaction และแสดงปุ่ม; เมื่อ handoff สำเร็จจะข้ามบทสนทนา Agent สำหรับภาพนั้น
- ใช้ [accounting-button-flow](../accounting-button-flow/SKILL.md) สำหรับขอบเขตปุ่ม state และการยืนยัน ห้ามสร้าง wizard หรือ pending ซ้อน
- ห้ามใช้ Vision หรือเรียก OCR ซ้ำเมื่อมีผล AksonOCR แล้ว

CLI adapter เดิมอยู่ที่ `/data/skills/accounting/process-slip-pipeline/scripts/process_slip.py` และใช้ `https://backend.aksonocr.com/api/v2/upload` ผ่าน `AKSONOCR_API_KEY`; ไม่ใช่ fallback ที่ Agent เรียกซ้ำเมื่อ Telegram handoff ล้มเหลว

## เมื่อส่งต่อไม่สำเร็จ

หากได้รับ `[AksonOCR Slip Result]` พร้อม `handoff_failed` ให้ใช้เฉพาะผล OCR ที่มีเพื่ออธิบายปัญหา แจ้งว่าส่งต่อเข้า flow ไม่สำเร็จและยังยืนยันสถานะบันทึกไม่ได้ ต้องตรวจรายการเดิมก่อนดำเนินการต่อ ห้ามขอ Confirm เปิด wizard สั่งส่งสลิปซ้ำ หรืออ้างว่ารายการยังไม่ถูกสร้าง

## เจ้าของการเขียน

การยืนยันและบันทึกเป็นหน้าที่ของ durable controller / `ProductionSavePipeline` เท่านั้น ห้าม Agent สร้าง Users/Projects อัตโนมัติ upload Drive หรือ append Transactions เอง แม้ผู้ใช้ตอบยืนยันในบทสนทนา เส้นทาง save ปัจจุบันยังไม่มีขั้นตอนสร้าง master data
