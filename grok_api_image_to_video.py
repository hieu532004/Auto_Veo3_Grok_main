"""
Grok Image-to-Video API — Direct HTTP approach (ported from AutoGrok I2VService).

Instead of running fetch() inside the browser via page.evaluate(),
this module makes direct HTTP calls using httpx with captured headers/cookies.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from grok_api_text_to_video import (
    ASSETS_BASE,
    GROK_BASE,
    GrokSession,
    MAX_RETRIES,
    RETRY_DELAY_BASE,
    download_video,
    upscale_video,
)
from watermark_remover import remove_watermark

# ── Constants ──────────────────────────────────────────────────────────
ENDPOINT_UPLOAD_FILE = f"{GROK_BASE}/rest/app-chat/upload-file"
ENDPOINT_POST_CREATE = f"{GROK_BASE}/rest/media/post/create"
ENDPOINT_CONVO_NEW = f"{GROK_BASE}/rest/app-chat/conversations/new"
ENDPOINT_UPSCALE = f"{GROK_BASE}/rest/media/video/upscale"


# ── Config ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ImageToVideoConfig:
    aspect_ratio: str = "9:16"
    video_length_seconds: int = 6
    resolution_name: str = "480p"
    is_video_edit: bool = False

    def as_dict(self) -> dict[str, Any]:
        resolution = str(self.resolution_name or "480p").strip().lower()
        if resolution not in {"480p", "720p"}:
            resolution = "480p"
        return {
            "aspectRatio": self.aspect_ratio,
            "videoLength": int(self.video_length_seconds),
            "resolutionName": resolution,
            "isVideoEdit": self.is_video_edit,
        }


# ── Helper Functions ───────────────────────────────────────────────────
def image_to_base64(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_mime_type(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(image_path))
    return mime or "image/png"


def _extract_user_id_from_file_uri(file_uri: str | None) -> str:
    raw = str(file_uri or "").strip().lstrip("/")
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 2 and parts[0] == "users":
        return parts[1]
    return ""


def _extract_user_and_generated_from_video_url(video_url: str | None) -> tuple[str, str]:
    raw = str(video_url or "").strip()
    if not raw:
        return "", ""
    m = re.search(r"/users/([^/]+)/generated/([^/]+)/", raw)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _build_generated_video_urls(user_id: str, generated_id: str) -> dict[str, str]:
    uid = str(user_id or "").strip()
    gid = str(generated_id or "").strip()
    if not uid or not gid:
        return {"direct": "", "hd": ""}
    base = f"https://assets.grok.com/users/{uid}/generated/{gid}"
    return {
        "direct": f"{base}/generated_video.mp4?cache=1&dl=1",
        "hd": f"{base}/generated_video_hd.mp4?cache=1&dl=1",
    }


# ── API: Upload Image ─────────────────────────────────────────────────
async def upload_image(image_path: Path, session: GrokSession) -> dict:
    """Upload an image file to Grok and get fileMetadataId."""
    try:
        content_b64 = image_to_base64(image_path)
        mime_type = get_mime_type(image_path)
        ext = image_path.suffix.lower().lstrip(".")
        file_name = f"{uuid.uuid4()}.{ext if ext != 'jpg' else 'jpeg'}"
    except Exception as e:
        return {"error": f"Failed to read image: {e}"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                ENDPOINT_UPLOAD_FILE,
                json={
                    "fileName": file_name,
                    "fileMimeType": mime_type,
                    "content": content_b64,
                    "fileSource": "IMAGINE_SELF_UPLOAD_FILE_SOURCE",
                },
                headers=session.build_headers(referer=f"{GROK_BASE}/imagine"),
            )

        if res.status_code != 200:
            return {
                "error": f"upload HTTP {res.status_code}",
                "errorDetail": res.text[:500],
            }

        data = res.json()
        file_metadata_id = data.get("fileMetadataId")
        file_uri = data.get("fileUri")

        if not file_metadata_id:
            return {"error": "no fileMetadataId", "errorDetail": json.dumps(data)[:500]}

        print(f"[I2V-API] ✅ Upload OK: {file_metadata_id}")
        return {"fileMetadataId": file_metadata_id, "fileUri": file_uri, "data": data}
    except Exception as e:
        return {"error": str(e)}


# ── API: Create Media Post ─────────────────────────────────────────────
async def create_media_post(image_url: str, session: GrokSession) -> dict:
    """Create media post (required step before I2V generation)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                ENDPOINT_POST_CREATE,
                json={
                    "mediaType": "MEDIA_POST_TYPE_IMAGE",
                    "mediaUrl": image_url,
                },
                headers=session.build_headers(referer=f"{GROK_BASE}/imagine"),
            )

        if res.status_code != 200:
            return {
                "error": f"post/create HTTP {res.status_code}",
                "errorDetail": res.text[:500],
            }

        data = res.json()
        post_id = data.get("post", {}).get("id")
        print(f"[I2V-API] ✅ post/create OK: postId={post_id}")
        return {"postId": post_id, "data": data}
    except Exception as e:
        return {"error": str(e)}

async def create_text_post(prompt: str, session: GrokSession) -> dict:
    """Create a media post for TEXT-TO-VIDEO generation."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                ENDPOINT_POST_CREATE,
                json={"mediaType": "MEDIA_POST_TYPE_VIDEO", "prompt": prompt},
                headers=session.build_headers(),
            )
        if res.status_code != 200:
            return {"error": f"create_text_post HTTP {res.status_code}"}
        post_id = res.json().get("post", {}).get("id")
        return {"postId": post_id}
    except Exception as e:
        return {"error": str(e)}


# ── API: Generate I2V (streaming) ─────────────────────────────────────
def _build_i2v_body(
    prompt: str,
    file_metadata_ids: list[str],
    image_names_urls: list[tuple[str, str]],
    cfg: ImageToVideoConfig,
    parent_post_id: str,
) -> dict:
    """Build the conversation/new request body for I2V generation."""
    
    modified_prompt = prompt
    images_ref = []
    
    # We need to preserve the original 1-indexed order based on the initial list
    # so that @Image1 corresponds to image_names_urls[0], etc.
    images_ref = []
    modified_prompt = prompt
    
    # Sort names by length descending to prevent partial replacements (e.g. nv1, nv10)
    sorted_names_with_idx = sorted([(i, name, url) for i, (name, url) in enumerate(image_names_urls) if name], key=lambda x: len(x[1]), reverse=True)
    
    import re
    for idx, name, url in sorted_names_with_idx:
        # Thay thế nv1 thành @Image1 (chú ý: idx + 1 để 1-indexed)
        pattern = re.compile(rf'(@)?\b{re.escape(name)}\b', re.IGNORECASE)
        modified_prompt = pattern.sub(f"@Image{idx + 1}", modified_prompt)
        
    # Populate images_ref strictly in the original order matching @Image1, @Image2...
    for name, url in image_names_urls:
        images_ref.append(url)

    message = f"{modified_prompt} --mode=custom"



    if len(image_names_urls) <= 1:
        # Standard Single Image-To-Video
        model_map_config = {
            "parentPostId": parent_post_id,
            **cfg.as_dict(),
        }
    else:
        # Multi-Character Reference-To-Video (from intercepted payload)
        model_map_config = {
            "parentPostId": parent_post_id,
            "isReferenceToVideo": True,
            "imageReferences": images_ref,
            **cfg.as_dict(),
        }

    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": message,
        "fileAttachments": file_metadata_ids,
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {
                "modelMap": {
                    "videoGenModelConfig": model_map_config
                }
            },
        },
    }


async def generate_i2v(
    image_path: Path,
    prompt: str,
    session: GrokSession,
    cfg: ImageToVideoConfig,
    on_progress: Callable[[int, int], None] | None = None,
    job_index: int = 0,
    on_status: Callable[[int, str], None] | None = None,
) -> dict:
    """
    Generate a single image-to-video via direct HTTP.
    Steps: upload image → create media post → stream conversation → parse result.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            suffix = f" (retry {attempt}/{MAX_RETRIES})" if attempt > 0 else ""

            parsed_paths = []
            for raw_p in str(image_path).split("|"):
                raw_p = raw_p.strip()
                if not raw_p: continue
                if "=" in raw_p:
                    pname, ppath = raw_p.split("=", 1)
                    parsed_paths.append((pname.strip(), Path(ppath.strip())))
                else:
                    parsed_paths.append(("", Path(raw_p)))

            if not parsed_paths:
                return {"error": "no valid images selected"}
                
            file_metadata_ids = []
            image_names_urls = []
            
            for p_idx, (p_name, p_path) in enumerate(parsed_paths):
                msg = f"Tải NV {p_idx+1}/{len(parsed_paths)}{suffix}"
                if on_status:
                    on_status(job_index, msg)
                print(f"[I2V-API] 📤 Uploading image {p_idx+1}/{len(parsed_paths)} ({p_name or 'unnamed'}): {p_path.name}...{suffix}")
                upload = await upload_image(p_path, session)

                if upload.get("error"):
                    if "HTTP 403" in str(upload.get("error", "")) and attempt < MAX_RETRIES:
                        print(f"[I2V-API] ⚠️ 403 from upload, will retry...")
                        await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                        continue # type: ignore
                    return {
                        "prompt": prompt,
                        "imagePath": str(image_path),
                        "videoUrl": None,
                        "progress": 0,
                        "error": upload.get("error"),
                    }

                file_metadata_id = upload["fileMetadataId"]
                file_uri = upload.get("fileUri", "")
                image_url = f"{ASSETS_BASE}{file_uri}" if file_uri else None
                
                file_metadata_ids.append(file_metadata_id)

                print(f"[I2V-API] 📝 Creating media post {p_idx+1}/{len(parsed_paths)}...")
                if image_url:
                    image_names_urls.append((p_name, image_url))
                    
            if len(parsed_paths) <= 1:
                # Standard Image-to-Video: animates exactly the first provided reference
                print(f"[I2V-API] 📝 Creating media post for standard I2V...")
                post = await create_media_post(image_names_urls[0][1], session)
                if post.get("error"):
                    if "HTTP 403" in str(post.get("error", "")) and attempt < MAX_RETRIES:
                        print(f"[I2V-API] ⚠️ 403 from createMediaPost, will retry...")
                        await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                        continue # type: ignore
                    return {"prompt": prompt, "imagePath": str(image_path), "videoUrl": None, "progress": 0, "error": post.get("error")}
                auto_parent_post_id = file_metadata_ids[0]
            else:
                # Text-to-Video Multi-Character Reference: requires text post base
                print(f"[I2V-API] 📝 Creating text post for multi-character T2V reference...")
                post = await create_text_post(prompt, session)
                if post.get("error"):
                    if "HTTP 403" in str(post.get("error", "")) and attempt < MAX_RETRIES:
                        print(f"[I2V-API] ⚠️ 403 from create_text_post, will retry...")
                        await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                        continue # type: ignore
                    return {"prompt": prompt, "imagePath": str(image_path), "videoUrl": None, "progress": 0, "error": post.get("error")}
                auto_parent_post_id = post.get("postId", "")

            # Step 3: Generate video (streaming)
            print(f"[I2V-API] 🎬 Generating video (stream) using parentPostId: {auto_parent_post_id}")
            body = _build_i2v_body(prompt, file_metadata_ids, image_names_urls, cfg, auto_parent_post_id)
            headers = session.build_headers(referer=f"{GROK_BASE}/imagine")

            result = {
                "prompt": prompt,
                "imagePath": str(image_path),
                "title": "",
                "videoUrl": None,
                "videoId": None,
                "userId": None,
                "progress": 0,
                "error": None,
                "fileMetadataId": file_metadata_ids[0] if file_metadata_ids else "",
                "convoStatus": 0,
            }

            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
                async with client.stream(
                    "POST",
                    ENDPOINT_CONVO_NEW,
                    json=body,
                    headers=headers,
                ) as response:
                    result["convoStatus"] = response.status_code

                    if response.status_code == 429 and attempt < MAX_RETRIES:
                        wait = RETRY_DELAY_BASE * (attempt + 1) + 5
                        print(f"[I2V-API] ⚠️ Rate limited (429), retry in {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    if response.status_code == 403 and attempt < MAX_RETRIES:
                        print(f"[I2V-API] ⚠️ 403 Forbidden, retry...")
                        await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                        continue

                    if response.status_code != 200:
                        body_text = ""
                        async for chunk in response.aiter_text():
                            body_text += chunk
                            if len(body_text) > 500:
                                break
                        result["error"] = f"HTTP {response.status_code}"
                        result["errorDetail"] = body_text[:500]
                        return result

                    # Parse streaming NDJSON
                    buffer = ""
                    last_logged = 0
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        lines = buffer.split("\n")
                        buffer = lines.pop()

                        for line in lines:
                            if not line.strip():
                                continue
                            try:
                                j = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            # Title
                            if j.get("result", {}).get("title", {}).get("newTitle"):
                                result["title"] = j["result"]["title"]["newTitle"]

                            # Errors
                            for err_src in [
                                j.get("error"),
                                j.get("result", {}).get("error"),
                                j.get("result", {}).get("response", {}).get("modelResponse", {}).get("error"),
                            ]:
                                if err_src and not result.get("error"):
                                    result["error"] = str(err_src) if isinstance(err_src, str) else json.dumps(err_src)[:200]

                            # Content blocking
                            mr = j.get("result", {}).get("response", {}).get("modelResponse", {})
                            if (mr.get("isSoftBlock") or mr.get("isDisallowed")) and not result.get("error"):
                                result["error"] = f"Content blocked"

                            # Video progress
                            vr = j.get("result", {}).get("response", {}).get("streamingVideoGenerationResponse")
                            if vr:
                                if vr.get("videoId"):
                                    result["videoId"] = vr["videoId"]
                                if vr.get("assetId") and not result.get("videoId"):
                                    result["videoId"] = vr["assetId"]
                                if vr.get("imageReference") and not result.get("userId"):
                                    m = re.search(r"/users/([^/]+)/", vr["imageReference"])
                                    if m:
                                        result["userId"] = m.group(1)

                                pct = vr.get("progress", result.get("progress", 0))
                                if pct > result.get("progress", 0):
                                    result["progress"] = pct
                                    if pct - last_logged >= 20:
                                        print(f"[I2V-API] Progress: {pct}%")
                                        last_logged = pct
                                        if on_progress:
                                            on_progress(job_index, pct)

                                if vr.get("videoUrl"):
                                    result["videoUrl"] = vr["videoUrl"]
                                    print(f"[I2V-API] 🎉 Video ready! url={result['videoUrl'][:50]}")

                    # Process remaining buffer
                    if buffer.strip():
                        try:
                            j = json.loads(buffer)
                            vr = j.get("result", {}).get("response", {}).get("streamingVideoGenerationResponse")
                            if vr:
                                result["progress"] = vr.get("progress", result.get("progress", 0))
                                if vr.get("videoUrl"):
                                    result["videoUrl"] = vr["videoUrl"]
                        except json.JSONDecodeError:
                            pass

            # Fallback
            if not result.get("videoUrl") and result.get("videoId"):
                result["videoUrl"] = result["videoId"]
                print(f"[I2V-API] Using videoId as download key: {result['videoId']}")

            if not result.get("videoUrl") and not result.get("error"):
                result["error"] = f"Video gen stopped at {result.get('progress', 0)}% - no video URL"

            return result

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY_BASE * (attempt + 1)
                print(f"[I2V-API] ⚠️ Network error: {e}, retry in {wait}s...")
                await asyncio.sleep(wait)
                continue
            return {
                "prompt": prompt,
                "imagePath": str(image_path),
                "videoUrl": None,
                "progress": 0,
                "error": str(e),
            }


# ── Batch Processing ──────────────────────────────────────────────────
async def run_batch_image_to_video(
    items: list[dict],
    session: GrokSession,
    cfg: ImageToVideoConfig,
    concurrency: int = 3,
    download_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_status: Callable[[int, str], None] | None = None,
    on_video: Callable[[int, str], None] | None = None,
    on_info: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    Run multiple I2V generations concurrently.
    items: list of {"image_path": str, "prompt": str}
    """
    N = len(items)
    sem = asyncio.Semaphore(max(1, min(concurrency, 5)))  # Cap at 5 for I2V
    results: list[dict] = [None] * N  # type: ignore

    def _safe(cb, *args):
        try:
            if cb:
                cb(*args)
        except Exception:
            pass

    async def process_one(idx: int, item: dict):
        async with sem:
            image_path_str = str(item.get("image_path") or item.get("image_link") or "").strip()
            prompt = str(item.get("prompt") or "").strip()

            if not image_path_str:
                _safe(on_status, idx, "Lỗi: không có ảnh")
                results[idx] = {"error": "no image path"}
                return

            image_path = Path(image_path_str)
            _safe(on_status, idx, "Chuẩn bị tải ảnh")
            _safe(on_info, f"[GROK-I2V {idx+1}] bắt đầu job")

            result = await generate_i2v(image_path, prompt, session, cfg, on_progress, idx, on_status)

            convo_status = result.get("convoStatus", 0)
            if convo_status == 403:
                _safe(on_status, idx, "Lỗi 403")
                _safe(on_info, f"[GROK-I2V {idx+1}] lỗi 403 - cần login lại")
                results[idx] = result
                return

            if result.get("error") and not result.get("videoUrl"):
                _safe(on_status, idx, "Lỗi")
                _safe(on_info, f"[GROK-I2V {idx+1}] lỗi: {result.get('error', '')[:80]}")
                results[idx] = result
                return

            pct = int(result.get("progress", 0))
            if pct >= 100:
                _safe(on_status, idx, "Tạo xong")
                _safe(on_progress, idx, 100)

            # Upscale
            parent_id = result.get("fileMetadataId", "")
            is_720p = str(cfg.resolution_name or "").lower() == "720p"
            hd_url = None

            if pct >= 100 and parent_id and not is_720p:
                _safe(on_status, idx, "Đang upscale")
                hd_url = await upscale_video(parent_id, session)
                if hd_url:
                    result["hdMediaUrl"] = hd_url

            # Download
            source_url = hd_url or result.get("videoUrl")
            # Build direct URL from userId/videoId if available
            if not source_url:
                user_id = result.get("userId", "")
                video_id = result.get("videoId", "")
                if user_id and video_id:
                    urls = _build_generated_video_urls(user_id, video_id)
                    source_url = urls.get("direct")

            if source_url and download_dir:
                _safe(on_status, idx, "Tải video")
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                pr_short = re.sub(r"[^A-Za-z0-9._-]+", "_", (prompt[:40] or "")).strip("._- ")
                filename = f"{idx+1:03d}_i2v_{pr_short}_{ts}.mp4"
                out_path = download_dir / filename

                ok = await download_video(source_url, session, out_path)
                if ok:
                    result["savedFile"] = str(out_path)
                    _safe(on_video, idx, str(out_path))
                    _safe(on_status, idx, "Hoàn thành")
                    _safe(on_info, f"[GROK-I2V {idx+1}] hoàn thành")
                else:
                    # Retry with fresh upscale
                    if parent_id:
                        fresh_hd = await upscale_video(parent_id, session)
                        if fresh_hd:
                            ok2 = await download_video(fresh_hd, session, out_path)
                            if ok2:
                                result["savedFile"] = str(out_path)
                                _safe(on_video, idx, str(out_path))
                                _safe(on_status, idx, "Hoàn thành")
                                results[idx] = result
                                return
                    _safe(on_status, idx, "Lỗi tải")
            else:
                if convo_status == 200 and pct >= 100:
                    _safe(on_status, idx, "Hoàn thành")
                else:
                    _safe(on_status, idx, f"Lỗi {convo_status}")

            results[idx] = result

    tasks = [asyncio.create_task(process_one(i, item)) for i, item in enumerate(items)]
    await asyncio.gather(*tasks, return_exceptions=True)

    return [r or {"error": "task failed"} for r in results]


# ── Legacy compatibility ───────────────────────────────────────────────
# Keep backward-compatible function signatures for existing callers

def payload_upload_image(image_path: Path) -> dict[str, Any]:
    return {
        "fileName": image_path.name,
        "fileMimeType": get_mime_type(image_path),
        "content": image_to_base64(image_path),
        "fileSource": "IMAGINE_SELF_UPLOAD_FILE_SOURCE",
    }


def payload_image_to_video(prompt: str, file_metadata_id: str, file_uri: str, cfg: ImageToVideoConfig) -> dict[str, Any]:
    asset_url = f"https://assets.grok.com/{file_uri}"
    message = f"{asset_url}  {prompt}"
    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": message,
        "fileAttachments": [file_metadata_id],
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {
                "modelMap": {
                    "videoGenModelConfig": {
                        "parentPostId": file_metadata_id,
                        **cfg.as_dict(),
                    }
                }
            },
        },
    }


def payload_upscale(video_id: str) -> dict[str, Any]:
    return {"videoId": video_id}


# Legacy page.evaluate wrappers - now redirect to direct HTTP
async def api_upload_image_in_page(page, image_path: Path, statsig_headers: dict) -> dict:
    """Legacy wrapper - now uses direct HTTP."""
    from grok_api_text_to_video import GrokSession
    cookies = await page.context.cookies(GROK_BASE)
    session = GrokSession(
        captured_headers=statsig_headers,
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
    )
    return await upload_image(image_path, session)


async def api_image_to_video_in_page(
    page, prompt: str, file_metadata_id: str, file_uri: str,
    parent_post_id: str | None, statsig_headers: dict,
    cfg: ImageToVideoConfig, timeout_seconds: int, job_index: int = 0,
) -> dict:
    """Legacy wrapper - now uses direct HTTP."""
    from grok_api_text_to_video import GrokSession
    cookies = await page.context.cookies(GROK_BASE)
    session = GrokSession(
        captured_headers=statsig_headers,
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
    )
    image_path = Path("")  # Not available in legacy call
    result = await generate_i2v(image_path, prompt, session, cfg, job_index=job_index)
    # Map to legacy format
    last_event = {
        "progress": result.get("progress", 0),
        "videoUrl": result.get("videoUrl"),
        "videoId": result.get("videoId"),
    }
    return {
        "status": result.get("convoStatus", 0),
        "lastEvent": last_event,
        "userId": result.get("userId"),
        "generatedId": result.get("videoId"),
        "directVideoUrl": "",
        "hdVideoUrlCandidate": "",
    }


async def api_create_image_post_in_page(page, media_url: str, statsig_headers: dict, job_index: int = 0) -> dict:
    """Legacy wrapper - now uses direct HTTP."""
    from grok_api_text_to_video import GrokSession
    cookies = await page.context.cookies(GROK_BASE)
    session = GrokSession(
        captured_headers=statsig_headers,
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
    )
    return await create_media_post(media_url, session)


async def api_upscale_video_in_page(page, video_id: str, statsig_headers: dict, job_index: int = 0, max_retries: int = 3) -> dict:
    """Legacy wrapper - now uses direct HTTP."""
    from grok_api_text_to_video import GrokSession
    cookies = await page.context.cookies(GROK_BASE)
    session = GrokSession(
        captured_headers=statsig_headers,
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
    )
    hd_url = await upscale_video(video_id, session, max_retries)
    return {"status": 200 if hd_url else 0, "hdMediaUrl": hd_url, "attempt": 1}
