import os
import sys
import json
import requests
import urllib.parse
import importlib.util
import tempfile
from pathlib import Path


_STAGING_GUARD = None


def _staging_guard():
    global _STAGING_GUARD
    if _STAGING_GUARD is None:
        path = os.path.join(os.path.dirname(__file__), "staging_guard.py")
        spec = importlib.util.spec_from_file_location("lekza_slip_staging_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STAGING_GUARD = module
    return _STAGING_GUARD


def _telegram_bot_id(gateway):
    adapters = getattr(gateway, "adapters", None)
    if isinstance(adapters, dict):
        for key, adapter in adapters.items():
            platform = getattr(key, "value", key)
            if str(platform).lower() == "telegram":
                bot = getattr(adapter, "_bot", None) or getattr(adapter, "bot", None)
                return str(getattr(bot, "id", "") or "")
    return ""


def _authorize_runtime_actor(source, gateway):
    guard = _staging_guard()
    mode = guard.validate_runtime_environment()
    if mode == "staging":
        guard.validate_staging_ocr_actor(
            _telegram_bot_id(gateway),
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "user_id", "") or ""),
        )


def _materialize_media(image_path_or_url):
    """Return a durable local image path, downloading remote Telegram media."""
    value = str(image_path_or_url)
    if value.startswith("file://"):
        return urllib.parse.unquote(value[7:])
    if not value.startswith(("http://", "https://")):
        return value

    roots_value = str(os.environ.get("LEKZA_ALLOWED_UPLOAD_ROOTS") or "").strip()
    roots = [item.strip() for item in roots_value.split(os.pathsep) if item.strip()]
    if not roots:
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS is required for remote media")
    root = Path(roots[0]).expanduser()
    if not root.is_absolute():
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS must contain absolute paths")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)

    response = requests.get(value, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to download Telegram media: status {response.status_code}"
        )
    content = response.content
    max_bytes = int(os.environ.get("LEKZA_MAX_SLIP_BYTES", 10 * 1024 * 1024))
    if not content or len(content) > max_bytes:
        raise ValueError("Downloaded Telegram media size is not allowed")
    if content.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise ValueError("Downloaded Telegram media type is not allowed")

    fd, local_path = tempfile.mkstemp(
        prefix="telegram-slip-", suffix=suffix, dir=str(root)
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    except Exception:
        try:
            os.remove(local_path)
        except OSError:
            pass
        raise
    return local_path

def call_akson_ocr(image_path_or_url):
    api_key = os.getenv("AKSONOCR_API_KEY")
    if not api_key:
        return {"error": "AKSONOCR_API_KEY environment variable is missing"}

    url = "https://backend.aksonocr.com/api/v2/upload"
    headers = {
        "X-API-Key": api_key
    }

    target_path = image_path_or_url
    temp_downloaded = None

    if image_path_or_url.startswith("http://") or image_path_or_url.startswith("https://"):
        try:
            resp = requests.get(image_path_or_url, timeout=30)
            if resp.status_code == 200:
                import tempfile
                fd, temp_downloaded = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                with open(temp_downloaded, "wb") as f:
                    f.write(resp.content)
                target_path = temp_downloaded
            else:
                return {"error": f"Failed to download media from URL: status {resp.status_code}", "http_status": resp.status_code}
        except Exception as e:
            return {"error": f"Exception downloading media URL: {str(e)}"}

    if not os.path.exists(target_path):
        if temp_downloaded and os.path.exists(temp_downloaded):
            try:
                os.remove(temp_downloaded)
            except Exception:
                pass
        return {"error": f"Image file not found: {target_path}"}

    try:
        filename = os.path.basename(target_path)
        if not filename or filename == "":
            filename = "slip.jpg"
        with open(target_path, "rb") as f:
            files = {
                "file": (filename, f, "image/jpeg")
            }
            data = {
                "model": "AksonOCR-preview",
                "tokenConfidence": "true"
            }
            response = requests.post(url, headers=headers, files=files, data=data)
    except Exception as e:
        if temp_downloaded and os.path.exists(temp_downloaded):
            try:
                os.remove(temp_downloaded)
            except Exception:
                pass
        return {"error": f"Request exception: {str(e)}"}

    if temp_downloaded and os.path.exists(temp_downloaded):
        try:
            os.remove(temp_downloaded)
        except Exception:
            pass

    http_status = response.status_code
    if http_status >= 300:
        return {"error": f"AksonOCR API failed with status {http_status}: {response.text}", "http_status": http_status}

    try:
        result = response.json()
    except Exception as e:
        return {"error": f"Failed to parse JSON response: {str(e)}", "http_status": http_status}

    confidence = result.get("confidence")
    pages = result.get("pages", [])
    markdown_text = ""
    if pages and isinstance(pages, list):
        markdown_text = "\n".join([page.get("markdown", "") for page in pages if isinstance(page, dict)])

    usage = result.get("usage")

    return {
        "akson_called": True,
        "http_status": http_status,
        "confidence": confidence,
        "raw_ocr_text": markdown_text or result.get("text", ""),
        "parsed": result.get("parsed", result.get("data", {})),
        "usage": usage,
        "raw_response": result
    }

def register(ctx):
    def pre_gateway_dispatch_hook(event, gateway=None, session_store=None, **kwargs):
        downloaded_media = False
        target_path = None
        durable_handoff = False
        try:
            # 2. Platform from event.source.platform (handle Enum / string)
            source = getattr(event, "source", None)
            platform_val = None
            if source:
                p_attr = getattr(source, "platform", None)
                if p_attr is not None:
                    if hasattr(p_attr, "value"):
                        platform_val = p_attr.value
                    else:
                        platform_val = str(p_attr)

            if not platform_val and isinstance(event, dict):
                src = event.get("source", {})
                if isinstance(src, dict):
                    p_attr = src.get("platform")
                    if p_attr is not None:
                        platform_val = p_attr.value if hasattr(p_attr, "value") else str(p_attr)

            # 3. media_urls and media_types read directly from event
            media_urls = getattr(event, "media_urls", None)
            if media_urls is None and isinstance(event, dict):
                media_urls = event.get("media_urls", [])

            media_types = getattr(event, "media_types", None)
            if media_types is None and isinstance(event, dict):
                media_types = event.get("media_types", [])

            message_id = getattr(event, "message_id", None)
            if message_id is None and isinstance(event, dict):
                message_id = event.get("message_id")

            # 4. Check if platform is telegram
            if platform_val != "telegram":
                return None

            chosen_media = None
            if media_urls and isinstance(media_urls, list) and len(media_urls) > 0:
                for idx, url in enumerate(media_urls):
                    mtype = media_types[idx] if media_types and idx < len(media_types) else ""
                    if not mtype or "image" in mtype.lower() or url.lower().endswith(('.jpg', '.jpeg', '.png')):
                        chosen_media = url
                        break
                if not chosen_media:
                    chosen_media = media_urls[0]

            if not chosen_media:
                return None

            # Authorization and runtime selection must complete before logging,
            # media download, OCR, or any other integration side effect.
            _authorize_runtime_actor(source, gateway)

            log_dir = "/data/logs"
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "accounting-slip-bridge.log")
            log_entry = {
                "hook_fired": True,
                "platform": platform_val,
                "media_count": len(media_urls or []),
                "message_id": message_id
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

            downloaded_media = str(chosen_media).startswith(("http://", "https://"))
            target_path = _materialize_media(chosen_media)

            ocr_res = call_akson_ocr(target_path)
            
            # Log OCR result update
            ocr_log = {
                "hook_fired": True,
                "platform": platform_val,
                "media_is_remote": str(chosen_media).startswith(("http://", "https://")),
                "akson_called": ocr_res.get("akson_called", False),
                "confidence": ocr_res.get("confidence"),
                "http_status": ocr_res.get("http_status"),
                "error": ocr_res.get("error")
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(ocr_log, ensure_ascii=False) + "\n")

            if ocr_res.get("akson_called"):
                confidence = ocr_res.get("confidence")
                raw_text = ocr_res.get("raw_ocr_text", "")
                parsed_fields = ocr_res.get("parsed", {})
                usage = ocr_res.get("usage", {})

                handoff_error = None
                transaction_id = None
                try:
                    import lekza_accounting_transaction_buttons as telegram_buttons

                    handoff = telegram_buttons.handoff_ocr_result(
                        event,
                        gateway,
                        session_store,
                        source_image_path=target_path,
                        ocr_result=ocr_res,
                    )
                    transaction_id = handoff["transaction"]["transaction_id"]
                    durable_handoff = True
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "telegram_transaction_handoff": True,
                            "transaction_id": transaction_id,
                            "message_id": message_id,
                        }, ensure_ascii=False) + "\n")
                except Exception as exc:
                    handoff_error = type(exc).__name__
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "telegram_transaction_handoff_failed": True,
                            "error_type": handoff_error,
                        }, ensure_ascii=False) + "\n")
                    if downloaded_media and not durable_handoff:
                        try:
                            os.remove(target_path)
                        except OSError:
                            pass

                rewrite_text = f"""[AksonOCR Slip Result]
- OCR source: AksonOCR (Vision OCR is strictly prohibited for this slip)
- confidence: {confidence}
- raw_ocr_text: {raw_text}
- parsed fields: {json.dumps(parsed_fields, ensure_ascii=False)}
- usage: {json.dumps(usage, ensure_ascii=False)}
- status: waiting_for_confirm
- transaction_id: {transaction_id or "unavailable"}
- telegram_buttons: {"scheduled" if transaction_id else "unavailable"}

คำสั่งระบบสำหรับ Agent:
1. ห้ามใช้ Vision อ่านสลิปซ้ำ ให้สรุปรายการจากผล AksonOCR ด้านบนเท่านั้น
2. แสดงรายละเอียดรายการและยอดเงินให้ผู้ใช้ตรวจสอบ
3. ต้องถามผู้ใช้ให้กด Confirm หรือยืนยันก่อนดำเนินการใดๆ
4. ห้ามเขียนหรือบันทึกข้อมูลลง Google Drive หรือ Google Sheets จนกว่าจะได้รับการยืนยัน (Confirm) จากผู้ใช้ที่ถูกต้อง
"""
                return {
                    "action": "rewrite",
                    "text": rewrite_text
                }

            if downloaded_media:
                try:
                    os.remove(target_path)
                except OSError:
                    pass
            return {"action": "skip"}

        except Exception as e:
            if downloaded_media and target_path and not durable_handoff:
                try:
                    os.remove(target_path)
                except OSError:
                    pass
            try:
                log_dir = "/data/logs"
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, "accounting-slip-bridge.log"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "error": "accounting slip processing failed",
                        "error_type": type(e).__name__,
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
            return {"action": "skip"}

        return None

    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_hook)
