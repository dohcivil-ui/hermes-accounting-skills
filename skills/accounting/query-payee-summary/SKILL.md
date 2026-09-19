---
name: query-payee-summary
description: "Use for payee/payer totals; delegate read-only queries to conversational-accounting-agent."
version: 1.0.0
author: Lekza (Hermes Agent)
platforms: [linux]
metadata:
  hermes:
    category: accounting
    tags: [telegram, accounting, reporting, payee]
---

# query-payee-summary

ใช้เมื่อถามยอดคนรับเงิน / คนโอนเงิน เช่น "สรุปยอดช่างแมน" หรือ "ใครได้เงินไปบ้างงาน A"

- อ่านและใช้ [conversational-accounting-agent](../conversational-accounting-agent/SKILL.md) เป็นกติกากลางสำหรับช่วงเวลา context และการเรียก query module
- ใช้ `party_summary` พร้อมชื่อบุคคล (ถ้ามี), `party_role`, `metric` และโครงการตามคำถาม; คำถามต่อเนื่องใช้ `follow_up` ตามกติกากลาง
- สำหรับผล `party_summary` แสดงยอดและจำนวนจาก `result.summaries` โดยแยกคนรับ/คนโอน และระบุช่วงเวลาจาก `grounding`; ผล intent อื่นแสดงตามกติกากลาง
- ถ้าชื่อกำกวม ให้ถามจากตัวเลือกที่ query คืนมา; ถ้าไม่พบ ให้แจ้งข้อจำกัดของผลและถามชื่อหรือช่วงเวลา ห้ามเดา
- Module ยังไม่คืนรายละเอียดทุกรายการหรือยอดบุคคลแยกทุกโครงการ จึงห้ามสัญญาหรือสร้างข้อมูลเหล่านี้เอง
- ห้ามอ่าน Sheets แล้วรวมยอดเอง หรือเขียนข้อมูลผ่าน skill นี้
