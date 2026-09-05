import os
import sys
import json
import re
import requests
import urllib.parse
import importlib.util
import tempfile
import unicodedata
from pathlib import Path


_STAGING_GUARD = None
_MAX_DIAGNOSTIC_MESSAGE_LENGTH = 160
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|file)://[^\s,;]+")
_RAW_OCR_PATTERN = re.compile(r"(?is)\braw[ _-]?ocr(?:[ _-]?text)?\s*[:=].*$")
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b("
    r"token|secret|credential|authorization|api[ _-]?key|"
    r"google(?:[ _-]?(?:resource|drive|sheet|spreadsheet|folder))?[ _-]?id|"
    r"telegram[ _-]?id|chat[ _-]?id|user[ _-]?id|bot[ _-]?id|"
    r"drive[ _-]?id|spreadsheet[ _-]?id|folder[ _-]?id"
    r")\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|(?<![:/])/(?:data|tmp|var|home)/)[^\s,;]+"
)
_OPAQUE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"
)
_NUMERIC_ID_PATTERN = re.compile(r"(?<!\d)\d{5,}(?!\d)")
_REFERENCE_FALLBACK_KEYS = {
    "reference",
    "referenceno",
    "referencenumber",
    "refno",
    "transactionreference",
    "transactionref",
    "bankreference",
    "slipreference",
}
_MARKDOWN_EMPHASIS_OPEN_PATTERN = re.compile(
    r"(?<!\w)(?:\*{1,3}|_{1,3})(?=\S)"
)
_MARKDOWN_EMPHASIS_CLOSE_PATTERN = re.compile(
    r"(?<=\S)(?:\*{1,3}|_{1,3})(?!\w)"
)
_REFERENCE_TEXT_PATTERNS = (
    re.compile(
        r"(?im)\b(?:reference|ref(?!erence))(?:\s*(?:no|number))?"
        r"\s*[:#-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._/-]{2,127})"
    ),
    re.compile(
        r"(?m)(?:รหัสอ้างอิง|หมายเลขอ้างอิง|เลขที่รายการ)\s*[:：#-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._/-]{2,127})"
    ),
)
_THAI_AMOUNT_LABEL_PATTERN = "|".join(
    re.escape(unicodedata.normalize("NFKC", label))
    for label in ("จำนวนเงิน", "ยอดเงิน")
)
_AMOUNT_TEXT_PATTERN = re.compile(
    rf"(?im)(?:\bamount\b|{_THAI_AMOUNT_LABEL_PATTERN})\s*[:：]?\s*"
    r"(?:THB\s*|฿\s*)?"
    r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w.,])"
)
_AKSON_AMOUNT_EXTRACTION_URL = (
    "https://backend.aksonocr.com/api/v1/key-extract"
)
_AKSON_AMOUNT_EXTRACTION_TIMEOUT_SECONDS = 30
_AKSON_AMOUNT_CUSTOM_FIELDS = [{
    "key": "amount",
    "description": (
        "ยอดเงินที่โอนหรือชำระในสลิป "
        "ส่งคืนเฉพาะตัวเลข ไม่รวม THB, ฿ หรือ บาท"
    ),
}]
_AKSON_AMOUNT_EXTRACTION_INSTRUCTIONS = (
    "คืนค่า amount เป็นตัวเลขเท่านั้น ไม่รวม THB, ฿ หรือ บาท"
)


def _sanitized_error_message(exc):
    message = " ".join(str(exc).split())
    message = _RAW_OCR_PATTERN.sub("raw_ocr_text=<redacted>", message)
    message = _URL_PATTERN.sub("<redacted-url>", message)
    message = _SENSITIVE_VALUE_PATTERN.sub(r"\1=<redacted>", message)
    message = _ABSOLUTE_PATH_PATTERN.sub("<redacted-path>", message)
    message = _OPAQUE_ID_PATTERN.sub("<redacted-id>", message)
    message = _NUMERIC_ID_PATTERN.sub("<redacted-id>", message)
    message = message.strip()
    if not message:
        message = "<redacted>"
    return message[:_MAX_DIAGNOSTIC_MESSAGE_LENGTH]


def _structured_reference(value):
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return None
    reference = unicodedata.normalize("NFKC", str(value)).strip()
    reference = re.sub(r"\s+", "", reference)
    reference = reference.strip("-._/")
    if not 3 <= len(reference) <= 128:
        return None
    if not all(
        character.isalnum() or character in "-._/" for character in reference
    ):
        return None
    return reference


def _reference_from_mapping(mapping):
    if not isinstance(mapping, dict):
        return None
    for key, value in mapping.items():
        normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized_key in _REFERENCE_FALLBACK_KEYS:
            reference = _structured_reference(value)
            if reference:
                return reference
    return None


def _reference_from_text(ocr_result):
    texts = [ocr_result.get("raw_ocr_text"), ocr_result.get("text")]
    raw_response = ocr_result.get("raw_response")
    if isinstance(raw_response, dict):
        texts.extend((raw_response.get("text"), raw_response.get("raw_ocr_text")))
    for text in texts:
        if not isinstance(text, str):
            continue
        normalized_text = unicodedata.normalize("NFKC", text)
        normalized_text = _MARKDOWN_EMPHASIS_OPEN_PATTERN.sub(
            "", normalized_text
        )
        normalized_text = _MARKDOWN_EMPHASIS_CLOSE_PATTERN.sub(
            "", normalized_text
        )
        for pattern in _REFERENCE_TEXT_PATTERNS:
            match = pattern.search(normalized_text)
            if match:
                reference = _structured_reference(match.group(1))
                if reference:
                    return reference
    return None


def _amount_from_text(ocr_result):
    texts = [ocr_result.get("raw_ocr_text"), ocr_result.get("text")]
    raw_response = ocr_result.get("raw_response")
    if isinstance(raw_response, dict):
        texts.extend((raw_response.get("text"), raw_response.get("raw_ocr_text")))
        pages = raw_response.get("pages")
        if isinstance(pages, list):
            texts.extend(
                page.get("markdown") for page in pages if isinstance(page, dict)
            )
    for text in texts:
        if not isinstance(text, str):
            continue
        normalized_text = unicodedata.normalize("NFKC", text)
        normalized_text = _MARKDOWN_EMPHASIS_OPEN_PATTERN.sub("", normalized_text)
        normalized_text = _MARKDOWN_EMPHASIS_CLOSE_PATTERN.sub("", normalized_text)
        match = _AMOUNT_TEXT_PATTERN.search(normalized_text)
        if match:
            return match.group(1)
    return None


def _normalize_ocr_result_for_handoff(ocr_result):
    if not isinstance(ocr_result, dict):
        raise ValueError("OCR result must be a mapping")
    normalized = dict(ocr_result)
    parsed_value = ocr_result.get("parsed")
    parsed = dict(parsed_value) if isinstance(parsed_value, dict) else {}

    reference = _structured_reference(parsed.get("reference_no"))
    if reference is None:
        reference = _reference_from_mapping(parsed)
    if reference is None:
        reference = _reference_from_mapping(ocr_result)
    raw_response = ocr_result.get("raw_response")
    if reference is None and isinstance(raw_response, dict):
        for candidate in (
            raw_response.get("parsed"),
            raw_response.get("data"),
            raw_response,
        ):
            reference = _reference_from_mapping(candidate)
            if reference:
                break
    if reference is None:
        reference = _reference_from_text(ocr_result)
    if reference is not None:
        parsed["reference_no"] = reference
    amount = parsed.get("amount")
    if amount is None and isinstance(raw_response, dict):
        for candidate in (
            raw_response.get("parsed"),
            raw_response.get("data"),
        ):
            if isinstance(candidate, dict) and candidate.get("amount") is not None:
                amount = candidate["amount"]
                break
    if amount is None:
        amount = _amount_from_text(ocr_result)
    if amount is not None:
        parsed["amount"] = amount
    normalized["parsed"] = parsed
    return normalized


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


def _allowed_upload_roots():
    roots_value = str(os.environ.get("LEKZA_ALLOWED_UPLOAD_ROOTS") or "").strip()
    roots = [item.strip() for item in roots_value.split(os.pathsep) if item.strip()]
    if not roots:
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS is required for Telegram media")
    configured_roots = [Path(item).expanduser() for item in roots]
    if any(not root.is_absolute() for root in configured_roots):
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS must contain absolute paths")
    if any(root.is_symlink() for root in configured_roots):
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS must not contain symlinks")
    configured_roots[0].mkdir(parents=True, exist_ok=True)
    return [
        root.resolve(strict=index == 0)
        for index, root in enumerate(configured_roots)
    ]


def _image_suffix(content, source_suffix=None):
    if content.startswith(b"\xff\xd8\xff"):
        detected_type = "image/jpeg"
        suffix = ".jpg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_type = "image/png"
        suffix = ".png"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        detected_type = "image/webp"
        suffix = ".webp"
    else:
        raise ValueError("Telegram media type is not allowed")
    allowed_suffixes = {
        "image/jpeg": {".jpg", ".jpeg"},
        "image/png": {".png"},
        "image/webp": {".webp"},
    }
    if (
        source_suffix is not None
        and source_suffix.lower() not in allowed_suffixes[detected_type]
    ):
        raise ValueError("Telegram media extension does not match its type")
    return suffix


def _validate_media_content(content, source_suffix=None):
    max_bytes = int(os.environ.get("LEKZA_MAX_SLIP_BYTES", 10 * 1024 * 1024))
    if max_bytes <= 0:
        raise ValueError("LEKZA_MAX_SLIP_BYTES must be positive")
    if not content or len(content) > max_bytes:
        raise ValueError("Telegram media size is not allowed")
    return _image_suffix(content, source_suffix)


def _local_media_path(value):
    if value.startswith("file://"):
        value = urllib.parse.unquote(value[7:])
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Local Telegram media path must be absolute")
    if ".." in candidate.parts:
        raise ValueError("Local Telegram media path traversal is not allowed")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Local Telegram media path must not contain symlinks")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Local Telegram media file is not available") from exc
    if not resolved.is_file():
        raise ValueError("Local Telegram media must be a regular file")
    return resolved


def _materialize_media(image_path_or_url):
    """Return validated Telegram media beneath an approved runtime root."""
    value = str(image_path_or_url)
    roots = _allowed_upload_roots()

    if not value.startswith(("http://", "https://")):
        source = _local_media_path(value)
        max_bytes = int(os.environ.get("LEKZA_MAX_SLIP_BYTES", 10 * 1024 * 1024))
        with source.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        suffix = _validate_media_content(content, source.suffix)
        if any(source == root or root in source.parents for root in roots):
            return str(source)
        root = roots[0]
    else:
        root = roots[0]

        response = requests.get(value, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to download Telegram media: status {response.status_code}"
            )
        content = response.content
        suffix = _validate_media_content(content)

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


def call_akson_amount_extraction(image_path):
    """Return only the requested amount scalar, or None on any failure."""
    api_key = os.getenv("AKSONOCR_API_KEY")
    target_path = str(image_path)
    if not api_key or not os.path.exists(target_path):
        return None

    try:
        filename = os.path.basename(target_path) or "slip.jpg"
        with open(target_path, "rb") as stream:
            response = requests.post(
                _AKSON_AMOUNT_EXTRACTION_URL,
                headers={"X-API-Key": api_key},
                files={"file": (filename, stream, "image/jpeg")},
                data={
                    "customFields": json.dumps(
                        _AKSON_AMOUNT_CUSTOM_FIELDS, ensure_ascii=False
                    ),
                    "additionalInstructions": (
                        _AKSON_AMOUNT_EXTRACTION_INSTRUCTIONS
                    ),
                    "model": "AksonOCR-preview",
                },
                timeout=_AKSON_AMOUNT_EXTRACTION_TIMEOUT_SECONDS,
            )
        if response.status_code >= 300:
            return None
        result = response.json()
    except Exception:
        return None

    if not isinstance(result, dict) or result.get("success") is not True:
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    return data.get("amount")


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


def _duplicate_ingress_response(outcome):
    status = getattr(outcome, "status", "failed")
    transaction_id = getattr(outcome, "transaction_id", None)
    if status == "duplicate":
        detail = f" transaction_id={transaction_id}" if transaction_id else ""
        message = (
            "[Lekza Duplicate Slip]\n"
            f"พบสลิปนี้เป็นรายการเดิมแล้ว{detail}\n"
            "ห้ามเรียก OCR หรือบันทึก Drive/Sheets ซ้ำ"
        )
    elif status == "processing":
        message = (
            "[Lekza Duplicate Slip]\n"
            "สลิปนี้กำลังประมวลผลอยู่ ห้ามเรียก OCR หรือบันทึกซ้ำ"
        )
    elif status == "ambiguous":
        message = (
            "[Lekza OCR Reconciliation Required]\n"
            "พบ OCR claim ที่ผลลัพธ์ไม่แน่ชัดหลังหมด lease "
            "ระบบหยุดแบบ fail-closed และไม่เรียก OCR ซ้ำ"
        )
    else:
        message = (
            "[Lekza OCR Failed Closed]\n"
            "รายการนี้ไม่สามารถประมวลผลซ้ำอัตโนมัติได้ "
            "ห้ามบันทึก Drive/Sheets"
        )
    return {"action": "rewrite", "text": message}

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

            try:
                import lekza_accounting_transaction_buttons as telegram_buttons
            except ImportError:
                telegram_buttons = None
            ingress_enabled = telegram_buttons is not None and all(
                callable(getattr(telegram_buttons, name, None))
                for name in (
                    "lookup_ocr_ingress",
                    "obtain_ocr_ingress",
                    "find_ocr_duplicate_candidates",
                    "complete_ocr_ingress",
                )
            )
            if not ingress_enabled:
                raise RuntimeError("Durable OCR ingress protection is unavailable")
            if ingress_enabled:
                existing_ingress = telegram_buttons.lookup_ocr_ingress(
                    event, gateway
                )
                if existing_ingress is not None and existing_ingress.status != "resume":
                    return _duplicate_ingress_response(existing_ingress)

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

            media_is_remote = str(chosen_media).startswith(("http://", "https://"))
            downloaded_media = media_is_remote
            target_path = _materialize_media(chosen_media)
            if not media_is_remote:
                source_value = str(chosen_media)
                if source_value.startswith("file://"):
                    source_value = urllib.parse.unquote(source_value[7:])
                downloaded_media = (
                    Path(target_path).resolve()
                    != Path(source_value).expanduser().resolve()
                )

            ingress_outcome = None

            def read_normalized_ocr():
                result = call_akson_ocr(target_path)
                if not result.get("akson_called"):
                    return result
                normalized = _normalize_ocr_result_for_handoff(result)
                parsed = normalized["parsed"]
                if parsed.get("amount") is None:
                    try:
                        extracted_amount = call_akson_amount_extraction(target_path)
                    except Exception:
                        extracted_amount = None
                    if extracted_amount is not None:
                        parsed["amount"] = extracted_amount
                normalized["akson_called"] = True
                return normalized

            if ingress_enabled:
                ingress_outcome = telegram_buttons.obtain_ocr_ingress(
                    event,
                    gateway,
                    source_image_path=target_path,
                    ocr_reader=read_normalized_ocr,
                )
                if ingress_outcome.status != "ready":
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "hook_fired": True,
                            "platform": platform_val,
                            "media_is_remote": media_is_remote,
                            "ingress_status": ingress_outcome.status,
                        }, ensure_ascii=False) + "\n")
                    if downloaded_media:
                        try:
                            os.remove(target_path)
                        except OSError:
                            pass
                    return _duplicate_ingress_response(ingress_outcome)
                ocr_res = ingress_outcome.ocr_result
                candidates = telegram_buttons.find_ocr_duplicate_candidates(
                    ingress_outcome
                )
                exact_reference = next(
                    (
                        candidate for candidate in candidates
                        if "exact_reference" in candidate.get("reasons", ())
                    ),
                    None,
                )
                if exact_reference is not None:
                    telegram_buttons.complete_ocr_ingress(
                        ingress_outcome, exact_reference["transaction_id"]
                    )
                    ingress_outcome.transaction_id = exact_reference[
                        "transaction_id"
                    ]
                    ingress_outcome.status = "duplicate"
                    if downloaded_media:
                        try:
                            os.remove(target_path)
                        except OSError:
                            pass
                    return _duplicate_ingress_response(ingress_outcome)
                if candidates:
                    ocr_res["duplicate_candidate"] = candidates[0]
            
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
                    if telegram_buttons is None:
                        import lekza_accounting_transaction_buttons as telegram_buttons

                    normalized_ocr_res = (
                        ocr_res if ingress_outcome is not None
                        else _normalize_ocr_result_for_handoff(ocr_res)
                    )
                    parsed_fields = normalized_ocr_res["parsed"]
                    if ingress_outcome is None and parsed_fields.get("amount") is None:
                        try:
                            extracted_amount = call_akson_amount_extraction(
                                target_path
                            )
                        except Exception:
                            extracted_amount = None
                        if extracted_amount is not None:
                            parsed_fields["amount"] = extracted_amount
                    handoff = telegram_buttons.handoff_ocr_result(
                        event,
                        gateway,
                        session_store,
                        source_image_path=target_path,
                        ocr_result=normalized_ocr_res,
                    )
                    transaction_id = handoff["transaction"]["transaction_id"]
                    durable_handoff = True
                    if ingress_outcome is not None:
                        telegram_buttons.complete_ocr_ingress(
                            ingress_outcome, transaction_id
                        )
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "telegram_transaction_handoff": True,
                            "transaction_id": transaction_id,
                            "message_id": message_id,
                        }, ensure_ascii=False) + "\n")
                except Exception as exc:
                    if (
                        ingress_outcome is not None
                        and getattr(exc, "duplicate_kind", None)
                        in {"reference", "source"}
                    ):
                        existing_transaction_id = getattr(
                            exc, "existing_transaction_id", None
                        )
                        if existing_transaction_id:
                            telegram_buttons.complete_ocr_ingress(
                                ingress_outcome, existing_transaction_id
                            )
                            ingress_outcome.transaction_id = existing_transaction_id
                            ingress_outcome.status = "duplicate"
                        if downloaded_media:
                            try:
                                os.remove(target_path)
                            except OSError:
                                pass
                        return _duplicate_ingress_response(ingress_outcome)
                    handoff_error = type(exc).__name__
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "telegram_transaction_handoff_failed": True,
                            "error_type": handoff_error,
                            "error_message": _sanitized_error_message(exc),
                        }, ensure_ascii=False) + "\n")
                    if downloaded_media and not durable_handoff:
                        try:
                            os.remove(target_path)
                        except OSError:
                            pass

                if durable_handoff:
                    return {"action": "skip"}

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
