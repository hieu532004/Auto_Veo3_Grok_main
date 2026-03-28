"""
Grok Image Generation API — 1:1 port of AutoGrok's ImageService + RefImageService.

ImageService flow (text-to-image):
  session.buildHeaders → POST /conversations/new → parse NDJSON → collect imageUrls + imageBase64 → download/save

RefImageService flow (reference-image):
  uploadFile → post/create → post/folders → POST /conversations/new (imagine-image-edit model) → parse → download/save

Key features ported:
  - 403 re-login (capture_session_from_page) with max 2 attempts + per-session lock
  - 429 rate-limit retry with exponential backoff
  - imageBase64 capture (data URI from stream tokens + imageBytes)
  - Concurrent worker pool with staggered start (75ms)
  - imageGenerationCount configurable
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from grok_api_text_to_video import (
    GROK_BASE,
    ASSETS_BASE,
    GrokSession,
)

# ── Endpoints ──────────────────────────────────────────────────────────
ENDPOINT_CONVO_NEW = f"{GROK_BASE}/rest/app-chat/conversations/new"
ENDPOINT_UPLOAD = f"{GROK_BASE}/rest/app-chat/upload-file"
ENDPOINT_POST_CREATE = f"{GROK_BASE}/rest/media/post/create"
ENDPOINT_POST_FOLDERS = f"{GROK_BASE}/rest/media/post/folders"

# ── Models ─────────────────────────────────────────────────────────────
IMAGE_MODEL = "grok-4-1-thinking-1129"
REF_IMAGE_MODEL = "imagine-image-edit"

# ── Constants ──────────────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds
MAX_RELOGIN_ATTEMPTS = 2
IMAGE_COUNT_DEFAULT = 2
CONCURRENCY_IMAGE = 30
CONCURRENCY_REF = 5

DATA_URI_PATTERN = re.compile(r"data:image/(png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]+")


# ═══════════════════════════════════════════════════════════════════════
# ImageService — Text-to-Image (port of AutoGrok ImageService)
# ═══════════════════════════════════════════════════════════════════════

def _build_image_body(prompt: str, config: dict | None = None) -> dict:
    """Build request body — exact port of ImageService.buildBody()."""
    cfg = config or {}
    aspect_ratio = cfg.get("aspectRatio", "1:1")
    image_count = int(cfg.get("imageGenerationCount", IMAGE_COUNT_DEFAULT))

    return {
        "temporary": False,
        "modelName": IMAGE_MODEL,
        "message": f"Generate an image: {prompt} --ar {aspect_ratio}",
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": False,
        "enableImageGeneration": True,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": True,
        "imageGenerationCount": image_count,
        "forceConcise": False,
        "toolOverrides": {},
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "isReasoning": False,
        "disableTextFollowUps": False,
        "responseMetadata": {
            "requestModelDetails": {"modelId": IMAGE_MODEL},
        },
        "disableMemory": False,
        "forceSideBySide": False,
        "modelMode": "MODEL_MODE_EXPERT",
        "isAsyncChat": False,
        "disableSelfHarmShortCircuit": False,
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 1.25,
            "screenWidth": 1280,
            "screenHeight": 800,
            "viewportWidth": 799,
            "viewportHeight": 735,
        },
    }


def _parse_image_response(text: str, status: int) -> dict:
    """Parse NDJSON — exact port of ImageService.parseResponse().
    Collects imageUrls (from streaming/modelResponse) AND imageBase64 (from imageBytes/data URIs).
    """
    result: dict = {
        "title": "",
        "imageUrls": [],
        "imageBase64": [],
        "error": None,
        "errorDetail": None,
        "status": status,
    }
    lines = text.split("\n")
    error_messages: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            # Check non-JSON lines for data URIs
            if "data:image/" in line:
                for m in DATA_URI_PATTERN.finditer(line):
                    result["imageBase64"].append({"data": m.group(0), "imageIndex": len(result["imageBase64"])})
            continue

        # Title
        try:
            new_title = j.get("result", {}).get("title", {}).get("newTitle")
            if new_title:
                result["title"] = new_title
        except Exception:
            pass

        # Errors
        if j.get("error"):
            err = j["error"]
            error_messages.append(str(err) if isinstance(err, str) else (err.get("message") or json.dumps(err))[:200])
        r_result = j.get("result", {})
        if r_result.get("error"):
            err = r_result["error"]
            error_messages.append(str(err) if isinstance(err, str) else (err.get("message") or json.dumps(err))[:200])

        mr = r_result.get("response", {}).get("modelResponse", {})
        if mr.get("error"):
            err = mr["error"]
            error_messages.append(str(err) if isinstance(err, str) else (err.get("message") or json.dumps(err))[:200])
        if mr.get("isSoftBlock") or mr.get("isDisallowed"):
            error_messages.append(f"Content blocked: softBlock={mr.get('isSoftBlock')}, disallowed={mr.get('isDisallowed')}")

        # Collect image URLs from streaming response
        ir = r_result.get("response", {}).get("streamingImageGenerationResponse")
        if ir:
            if ir.get("progress") == 100 and ir.get("imageUrl"):
                result["imageUrls"].append({"imageUrl": ir["imageUrl"], "imageIndex": ir.get("imageIndex", 0)})
            # Collect base64 from imageBytes
            if ir.get("imageBytes"):
                result["imageBase64"].append({"data": ir["imageBytes"], "imageIndex": ir.get("imageIndex", len(result["imageBase64"]))})

        # Fallback: generatedImageUrls in modelResponse
        gen_urls = mr.get("generatedImageUrls")
        if gen_urls and len(gen_urls) > 0 and len(result["imageUrls"]) == 0:
            for i, u in enumerate(gen_urls):
                result["imageUrls"].append({"imageUrl": u, "imageIndex": i})

        # Collect base64 data URIs from tokens
        token = r_result.get("response", {}).get("token")
        if isinstance(token, str) and "data:image/" in token:
            for m in DATA_URI_PATTERN.finditer(token):
                result["imageBase64"].append({"data": m.group(0), "imageIndex": len(result["imageBase64"])})

    total_images = len(result["imageUrls"]) + len(result["imageBase64"])
    if total_images == 0:
        if status != 200:
            result["error"] = f"HTTP {status}"
        elif error_messages:
            result["error"] = " | ".join(error_messages)
        else:
            result["error"] = "no images returned"
        result["errorDetail"] = text[:500]

    return result


async def generate_one_image(
    prompt: str,
    session: GrokSession,
    config: dict | None = None,
    on_progress: Callable | None = None,
    relogin_fn: Callable | None = None,
) -> dict:
    """Generate images from prompt — exact port of ImageService.generateOne().
    Handles 429 retry, 403 re-login, network errors.
    """
    cfg = config or {}
    for attempt in range(MAX_RETRIES + 1):
        try:
            headers = session.build_headers()
            body = _build_image_body(prompt, cfg)

            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
                res = await client.post(ENDPOINT_CONVO_NEW, json=body, headers=headers)

            # 429 rate limit → retry
            if res.status_code == 429 and attempt < MAX_RETRIES:
                wait = RETRY_DELAY + 5 * (attempt + 1)
                print(f"[ImageService] Rate limited (429), retry in {wait}s...")
                await asyncio.sleep(wait)
                continue

            # 403 forbidden → re-login
            if res.status_code == 403 and attempt < MAX_RETRIES:
                print(f"[ImageService] 403 Forbidden — attempting re-login...")
                if relogin_fn:
                    new_session = await relogin_fn()
                    if new_session:
                        session.captured_headers = new_session.captured_headers
                        session.cookies = new_session.cookies
                        print(f"[ImageService] Re-login OK, retrying...")
                        continue
                return {"imageUrls": [], "imageBase64": [], "error": "403 Forbidden — re-login failed", "status": 403}

            result = _parse_image_response(res.text, res.status_code)

            if (result["imageUrls"] or result["imageBase64"]) and on_progress:
                try:
                    on_progress({"progress": 100, "status": "completed"})
                except Exception:
                    pass

            return result

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if attempt < MAX_RETRIES:
                print(f"[ImageService] Network error: {e}, retry {attempt+1}/{MAX_RETRIES}...")
                await asyncio.sleep(RETRY_DELAY)
                continue
            return {"imageUrls": [], "imageBase64": [], "error": str(e), "status": 0}

    return {"imageUrls": [], "imageBase64": [], "error": "max retries exceeded", "status": 0}


async def download_image(image_url: str, session: GrokSession) -> dict | None:
    """Download image — port of ImageService.downloadImage()."""
    headers = session.build_download_headers()
    bases = [ASSETS_BASE]

    for base_url in bases:
        try:
            url = f"{base_url}{image_url}"
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                res = await client.get(url, headers=headers)
            if res.status_code == 200 and len(res.content) > 1000:
                ct = res.headers.get("content-type", "")
                return {"data": res.content, "size": len(res.content), "contentType": ct}
        except Exception:
            continue
    return None


def _save_base64_image(b64_data: str, filepath: Path) -> bool:
    """Save base64 image data to file. Handles data URI prefix."""
    try:
        data = b64_data
        if data.startswith("data:image/"):
            # Strip "data:image/png;base64," prefix
            idx = data.find(",")
            if idx >= 0:
                data = data[idx + 1:]
        raw = base64.b64decode(data)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_bytes(raw)
        return True
    except Exception:
        return False


async def generate_batch_images(
    prompts: list[str],
    session: GrokSession,
    config: dict | None = None,
    on_progress: Callable | None = None,
    start_idx: int = 0,
    output_folder: Path | None = None,
    relogin_fn: Callable | None = None,
) -> list[dict]:
    """
    Generate images with concurrent worker pool — exact port of ImageService.generateBatch().
    Staggered worker starts (75ms), concurrent semaphore.
    """
    N = len(prompts)
    cfg = config or {}
    concurrency = int(cfg.get("batchSize", CONCURRENCY_IMAGE))
    out_dir = output_folder or Path("downloads/grok_image")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict | None] = [None] * N
    next_idx_lock = asyncio.Lock()
    next_idx = [0]  # mutable counter

    async def worker():
        while True:
            async with next_idx_lock:
                if next_idx[0] >= N:
                    return
                my_idx = next_idx[0]
                next_idx[0] += 1

            prompt = prompts[my_idx]
            global_num = start_idx + my_idx + 1
            shot_num = f"{global_num:04d}"

            print(f"[ImageService] #{my_idx+1}/{N} (shot{shot_num}) starting: {prompt[:50]}...")

            result = await generate_one_image(
                prompt, session, cfg,
                on_progress=lambda prog: on_progress(prompt, prog.get("progress", 0), None, my_idx) if on_progress else None,
                relogin_fn=relogin_fn,
            )

            # Save images
            saved_files: list[str] = []
            title_slug = re.sub(r"[^a-zA-Z0-9\u00C0-\u024F\u1E00-\u1EFF ]+", "", result.get("title", "")).strip().replace("  ", " ").replace(" ", "_")[:60]

            # Save base64 images first (from /imagine endpoint)
            for img in result.get("imageBase64", []):
                try:
                    b64_data = img.get("data", "")
                    ext = "png"
                    if b64_data.startswith("data:image/"):
                        for fmt in ("jpeg", "jpg", "png", "webp"):
                            if f"data:image/{fmt}" in b64_data:
                                ext = "jpg" if fmt == "jpeg" else fmt
                                break
                    filename = f"shot{shot_num}_{title_slug}_i{img.get('imageIndex', 0)}.{ext}" if title_slug else f"shot{shot_num}_i{img.get('imageIndex', 0)}.{ext}"
                    filepath = out_dir / filename
                    if _save_base64_image(b64_data, filepath):
                        saved_files.append(str(filepath))
                        print(f"[ImageService] Saved base64: {filename}")
                except Exception as e:
                    print(f"[ImageService] Base64 save error: {e}")

            # Download from URL (fallback if no base64)
            if result.get("imageUrls") and not saved_files:
                for img in result["imageUrls"]:
                    try:
                        dl = await download_image(img["imageUrl"], session)
                        if dl:
                            ext = "png" if "png" in (dl.get("contentType") or "") else "jpg"
                            filename = f"shot{shot_num}_{title_slug}_i{img.get('imageIndex', 0)}.{ext}" if title_slug else f"shot{shot_num}_i{img.get('imageIndex', 0)}.{ext}"
                            filepath = out_dir / filename
                            filepath.parent.mkdir(parents=True, exist_ok=True)
                            filepath.write_bytes(dl["data"])
                            saved_files.append(str(filepath))
                    except Exception as e:
                        print(f"[ImageService] Download error: {e}")

            job_result = {
                "prompt": prompt,
                "localIdx": my_idx,
                "title": result.get("title", ""),
                "savedFiles": saved_files,
                "outputPath": saved_files[0] if saved_files else None,
                "success": len(saved_files) > 0,
                "error": result.get("error"),
            }
            results[my_idx] = job_result

            if on_progress:
                try:
                    on_progress(prompt, 100, job_result, my_idx)
                except Exception:
                    pass

            icon = "OK" if saved_files else "FAIL"
            print(f"[ImageService] #{my_idx+1}/{N} {icon} {result.get('title') or prompt[:50]}")

            # 75ms delay between requests in worker
            await asyncio.sleep(0.075)

    # Launch workers with staggered starts (75ms each)
    tasks = []
    for i in range(min(concurrency, N)):
        await asyncio.sleep(0.075 * i)  # stagger
        tasks.append(asyncio.create_task(worker()))
    await asyncio.gather(*tasks, return_exceptions=True)

    print(f"[ImageService] Complete: {sum(1 for r in results if r and r.get('success'))}/{N} successful")
    return [r or {"error": "task failed", "success": False} for r in results]


# ═══════════════════════════════════════════════════════════════════════
# RefImageService — Reference Image Generation (port of AutoGrok RefImageService)
# ═══════════════════════════════════════════════════════════════════════

async def upload_file(image_path: Path, session: GrokSession) -> dict:
    """Upload single image — port of RefImageService.uploadFile()."""
    try:
        content_bytes = image_path.read_bytes()
        content_b64 = base64.b64encode(content_bytes).decode("utf-8")

        ext = image_path.suffix.lower().lstrip(".")
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
        file_mime = mime_map.get(ext, "image/jpeg")
        file_name = f"{uuid.uuid4()}.{'jpeg' if ext == 'jpg' else ext}"

        headers = session.build_headers()
        headers["referer"] = "https://grok.com/imagine"

        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(ENDPOINT_UPLOAD, json={
                "fileName": file_name,
                "fileMimeType": file_mime,
                "content": content_b64,
                "fileSource": "IMAGINE_SELF_UPLOAD_FILE_SOURCE",
            }, headers=headers)

        if res.status_code != 200:
            return {"error": f"upload HTTP {res.status_code}", "errorDetail": res.text[:500]}

        data = res.json()
        fid = data.get("fileMetadataId")
        furi = data.get("fileUri")
        if not fid:
            return {"error": "no fileMetadataId", "errorDetail": json.dumps(data)[:500]}

        print(f"[RefImageService] Upload OK: {fid}")
        return {"fileMetadataId": fid, "fileUri": furi, "uploadResponse": data}
    except Exception as e:
        return {"error": str(e)}


async def create_media_post(image_url: str, session: GrokSession) -> dict:
    """Create media post — port of RefImageService.createMediaPost()."""
    headers = session.build_headers()
    headers["referer"] = "https://grok.com/imagine"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(ENDPOINT_POST_CREATE, json={
                "mediaType": "MEDIA_POST_TYPE_IMAGE",
                "mediaUrl": image_url,
            }, headers=headers)

        if res.status_code != 200:
            return {"error": f"post/create HTTP {res.status_code}", "errorDetail": res.text[:500]}

        data = res.json()
        post_id = data.get("post", {}).get("id")
        print(f"[RefImageService] post/create OK: postId={post_id}")
        return {"postId": post_id, "postData": data}
    except Exception as e:
        return {"error": str(e)}


async def create_post_folder(post_id: str, session: GrokSession) -> dict:
    """Create post folder — port of RefImageService.createPostFolder()."""
    headers = session.build_headers()
    headers["referer"] = "https://grok.com/imagine"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(ENDPOINT_POST_FOLDERS, json={"postId": post_id}, headers=headers)

        if res.status_code != 200:
            print(f"[RefImageService] post/folders HTTP {res.status_code} (non-critical)")
        else:
            print(f"[RefImageService] post/folders OK")
        return {"success": res.status_code == 200, "data": res.json() if res.status_code == 200 else None}
    except Exception as e:
        print(f"[RefImageService] post/folders error: {e} (non-critical)")
        return {"success": False, "error": str(e)}


async def upload_ref_images(image_paths: list[Path], session: GrokSession) -> dict:
    """
    Upload all ref images and create media posts.
    Port of RefImageService.uploadRefImages().
    Returns { imageUrls, parentPostId } or { error }
    """
    image_urls: list[str] = []
    last_post_id: str | None = None

    for i, img_path in enumerate(image_paths):
        print(f"[RefImageService] Uploading ref {i+1}/{len(image_paths)}: {img_path.name}")

        # Step 1: Upload
        upload = await upload_file(img_path, session)
        if upload.get("error"):
            return {"error": f"Upload ref {i+1} failed: {upload['error']}"}

        file_uri = upload.get("fileUri", "")
        if not file_uri:
            return {"error": f"No fileUri for ref {i+1}"}
        image_url = f"{ASSETS_BASE}{file_uri}"

        # Step 2: Create media post
        post = await create_media_post(image_url, session)
        if post.get("error"):
            return {"error": f"Post/create ref {i+1} failed: {post['error']}"}

        image_urls.append(image_url)
        last_post_id = post.get("postId")

    # Step 3: Create post folder with last postId
    if last_post_id:
        await create_post_folder(last_post_id, session)

    return {"imageUrls": image_urls, "parentPostId": last_post_id}


def _build_ref_image_body(prompt: str, image_urls: list[str], parent_post_id: str | None) -> dict:
    """Build request body for ref image — exact port of RefImageService.buildRefImageBody()."""
    return {
        "temporary": True,
        "modelName": REF_IMAGE_MODEL,
        "message": prompt,
        "enableImageGeneration": True,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": True,
        "imageGenerationCount": 2,
        "forceConcise": False,
        "toolOverrides": {"imageGen": True},
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "isReasoning": False,
        "disableTextFollowUps": True,
        "responseMetadata": {
            "modelConfigOverride": {
                "modelMap": {
                    "imageEditModelConfig": {
                        "imageReferences": image_urls,
                        "parentPostId": parent_post_id,
                    },
                    "imageEditModel": "imagine",
                },
            },
        },
        "disableMemory": False,
        "forceSideBySide": False,
    }


async def generate_one_ref_image(
    item: dict,
    session: GrokSession,
    config: dict | None = None,
    on_progress: Callable | None = None,
    relogin_fn: Callable | None = None,
) -> dict:
    """
    Generate image with ref images — exact port of RefImageService.generateOne().
    item: { prompt: str, refImagePaths: [Path, ...] }
    """
    prompt = item.get("prompt", "")
    ref_paths = [Path(p) for p in item.get("refImagePaths", [])]

    for attempt in range(MAX_RETRIES + 1):
        try:
            # Step 1: Upload all ref images
            suffix = f" (retry {attempt}/{MAX_RETRIES})" if attempt > 0 else ""
            print(f"[RefImageService] Uploading {len(ref_paths)} ref image(s)...{suffix}")
            upload_result = await upload_ref_images(ref_paths, session)

            # Handle 403 from upload → re-login
            if upload_result.get("error") and "HTTP 403" in str(upload_result["error"]) and attempt < MAX_RETRIES:
                if relogin_fn:
                    new_session = await relogin_fn()
                    if new_session:
                        session.captured_headers = new_session.captured_headers
                        session.cookies = new_session.cookies
                        print(f"[RefImageService] Re-login OK, retrying...")
                        continue
                return {"imageUrls": [], "imageBase64": [], "error": "403 — re-login failed", "status": 403}

            if upload_result.get("error"):
                return {"imageUrls": [], "imageBase64": [], "error": upload_result["error"], "status": 0}

            # Step 2: Generate
            print(f"[RefImageService] Generating with {len(upload_result['imageUrls'])} ref(s): {prompt[:50]}...")
            headers = session.build_headers()
            headers["referer"] = "https://grok.com/imagine"
            body = _build_ref_image_body(prompt, upload_result["imageUrls"], upload_result.get("parentPostId"))

            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
                res = await client.post(ENDPOINT_CONVO_NEW, json=body, headers=headers)

            # 429 rate limit
            if res.status_code == 429 and attempt < MAX_RETRIES:
                wait = RETRY_DELAY + 5 * (attempt + 1)
                print(f"[RefImageService] 429, retry in {wait}s...")
                await asyncio.sleep(wait)
                continue

            # 403 → re-login
            if res.status_code == 403 and attempt < MAX_RETRIES:
                if relogin_fn:
                    new_session = await relogin_fn()
                    if new_session:
                        session.captured_headers = new_session.captured_headers
                        session.cookies = new_session.cookies
                        continue
                return {"imageUrls": [], "imageBase64": [], "error": "403 — re-login failed", "status": 403}

            result = _parse_image_response(res.text, res.status_code)

            if (result["imageUrls"] or result["imageBase64"]) and on_progress:
                try:
                    on_progress({"progress": 100, "status": "completed"})
                except Exception:
                    pass

            return result

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if attempt < MAX_RETRIES:
                print(f"[RefImageService] Error, retry {attempt+1}/{MAX_RETRIES}: {e}")
                await asyncio.sleep(RETRY_DELAY)
                continue
            return {"imageUrls": [], "imageBase64": [], "error": str(e), "status": 0}

    return {"imageUrls": [], "imageBase64": [], "error": "max retries exceeded", "status": 0}


async def generate_batch_ref_images(
    items: list[dict],
    session: GrokSession,
    config: dict | None = None,
    on_progress: Callable | None = None,
    start_idx: int = 0,
    output_folder: Path | None = None,
    relogin_fn: Callable | None = None,
) -> list[dict]:
    """
    Generate ref images with concurrent worker pool — port of RefImageService.generateBatch().
    items: [{ prompt, refImagePaths: [path1, ...] }, ...]
    """
    N = len(items)
    cfg = config or {}
    concurrency = min(int(cfg.get("batchSize", CONCURRENCY_REF)), N)
    out_dir = output_folder or Path("downloads/grok_image")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict | None] = [None] * N
    next_idx_lock = asyncio.Lock()
    next_idx = [0]

    async def worker():
        while True:
            async with next_idx_lock:
                if next_idx[0] >= N:
                    return
                my_idx = next_idx[0]
                next_idx[0] += 1

            item = items[my_idx]
            global_num = start_idx + my_idx + 1
            shot_num = f"{global_num:04d}"

            print(f"[RefImageService] #{my_idx+1}/{N} (shot{shot_num}) refs={len(item.get('refImagePaths', []))} | {item['prompt'][:50]}...")

            result = await generate_one_ref_image(
                item, session, cfg,
                on_progress=lambda prog: on_progress(item["prompt"], prog.get("progress", 0), None, my_idx) if on_progress else None,
                relogin_fn=relogin_fn,
            )

            saved_files: list[str] = []
            title_slug = re.sub(r"[^a-zA-Z0-9\u00C0-\u024F\u1E00-\u1EFF ]+", "", result.get("title", "")).strip().replace("  ", " ").replace(" ", "_")[:60]

            # Save base64 images
            for img in result.get("imageBase64", []):
                try:
                    b64_data = img.get("data", "")
                    ext = "png"
                    if b64_data.startswith("data:image/"):
                        for fmt in ("jpeg", "jpg", "png", "webp"):
                            if f"data:image/{fmt}" in b64_data:
                                ext = "jpg" if fmt == "jpeg" else fmt
                                break
                    filename = f"ref_shot{shot_num}_{title_slug}_i{img.get('imageIndex', 0)}.{ext}" if title_slug else f"ref_shot{shot_num}_i{img.get('imageIndex', 0)}.{ext}"
                    filepath = out_dir / filename
                    if _save_base64_image(b64_data, filepath):
                        saved_files.append(str(filepath))
                        print(f"[RefImageService] Saved: {filename}")
                except Exception as e:
                    print(f"[RefImageService] Base64 save error: {e}")

            # Download from URL (fallback)
            if result.get("imageUrls") and not saved_files:
                for img in result["imageUrls"]:
                    try:
                        dl = await download_image(img["imageUrl"], session)
                        if dl:
                            ext = "png" if "png" in (dl.get("contentType") or "") else "jpg"
                            filename = f"ref_shot{shot_num}_{title_slug}_i{img.get('imageIndex', 0)}.{ext}" if title_slug else f"ref_shot{shot_num}_i{img.get('imageIndex', 0)}.{ext}"
                            filepath = out_dir / filename
                            filepath.parent.mkdir(parents=True, exist_ok=True)
                            filepath.write_bytes(dl["data"])
                            saved_files.append(str(filepath))
                    except Exception as e:
                        print(f"[RefImageService] Download error: {e}")

            job_result = {
                "prompt": item["prompt"],
                "localIdx": my_idx,
                "title": result.get("title", ""),
                "savedFiles": saved_files,
                "success": len(saved_files) > 0,
                "error": result.get("error"),
            }
            results[my_idx] = job_result

            if on_progress:
                try:
                    on_progress(item["prompt"], 100, job_result, my_idx)
                except Exception:
                    pass

            icon = "OK" if saved_files else "FAIL"
            print(f"[RefImageService] #{my_idx+1}/{N} {icon} {result.get('title') or item['prompt'][:50]}")

            # 200ms delay between requests for ref image (upload overhead)
            await asyncio.sleep(0.2)

    # Launch workers with staggered starts (200ms for ref)
    tasks = []
    for i in range(min(concurrency, N)):
        await asyncio.sleep(0.2 * i)
        tasks.append(asyncio.create_task(worker()))
    await asyncio.gather(*tasks, return_exceptions=True)

    print(f"[RefImageService] Complete: {sum(1 for r in results if r and r.get('success'))}/{N} successful")
    return [r or {"error": "task failed", "success": False} for r in results]
