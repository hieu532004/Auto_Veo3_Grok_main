
async def request_upload_image_via_browser(page, payload, access_token):
    import json
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "X-Goog-AuthUser": "0",
        }
        data = json.dumps(payload)
        response = await page.request.post(
            URL_UPLOAD_IMAGE,
            data=data,
            headers=headers,
            timeout=60000,
        )
        body = await response.text()
        return {
            "ok": response.ok,
            "url": URL_UPLOAD_IMAGE,
            "status": response.status,
            "reason": response.status_text,
            "headers": dict(response.headers),
            "body": body,
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": URL_UPLOAD_IMAGE,
            "error": str(exc),
        }
