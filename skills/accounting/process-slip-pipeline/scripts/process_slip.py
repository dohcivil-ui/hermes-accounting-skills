import os
import sys
import requests
import json

def process_slip(image_path):
    api_key = os.getenv("AKSONOCR_API_KEY")
    if not api_key:
        print(json.dumps({"error": "AKSONOCR_API_KEY environment variable is missing"}))
        sys.exit(1)

    url = "https://backend.aksonocr.com/api/v2/upload"
    headers = {
        "X-API-Key": api_key
    }

    if not os.path.exists(image_path):
        print(json.dumps({"error": f"Image file not found: {image_path}"}))
        sys.exit(1)

    try:
        filename = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            files = {
                "file": (filename, f, "image/jpeg")
            }
            data = {
                "model": "AksonOCR-preview",
                "tokenConfidence": "true"
            }
            response = requests.post(url, headers=headers, files=files, data=data)
    except Exception as e:
        print(json.dumps({"error": f"Request exception: {str(e)}"}))
        sys.exit(1)

    if response.status_code >= 300:
        print(f"HTTP Status: {response.status_code}, Response Body: {response.text}", file=sys.stderr)
        print(json.dumps({"error": f"AksonOCR API failed with status {response.status_code}: {response.text}", "http_status": response.status_code}))
        sys.exit(1)

    try:
        result = response.json()
    except Exception as e:
        print(json.dumps({"error": f"Failed to parse JSON response: {str(e)}", "http_status": response.status_code}))
        sys.exit(1)

    confidence = result.get("confidence")
    pages = result.get("pages", [])
    markdown_text = ""
    if pages and isinstance(pages, list):
        markdown_text = "\n".join([page.get("markdown", "") for page in pages if isinstance(page, dict)])

    usage = result.get("usage")

    output = {
        "akson_called": True,
        "confidence": confidence,
        "raw_ocr_text": markdown_text or result.get("text", ""),
        "parsed": result.get("parsed", result.get("data", {})),
        "usage": usage,
        "raw_response": result
    }
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python3 process_slip.py <image_path>"}))
        sys.exit(1)
    process_slip(sys.argv[1])
