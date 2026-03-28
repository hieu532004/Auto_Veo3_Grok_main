"""
Grok Text-to-Video API — Direct HTTP approach (ported from AutoGrok).

Instead of running fetch() inside the browser via page.evaluate(),
this module makes direct HTTP calls using httpx with captured headers/cookies.
The browser is only used for authentication and header capture.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from grok_chrome_manager import open_chrome_session
from watermark_remover import remove_watermark
import httpx

# ── Constants ──────────────────────────────────────────────────────────
GROK_BASE = "https://grok.com"
ASSETS_BASE = "https://assets.grok.com/"

ENDPOINT_CREATE_POST = f"{GROK_BASE}/rest/media/post/create"
ENDPOINT_CONVO_NEW = f"{GROK_BASE}/rest/app-chat/conversations/new"
ENDPOINT_UPSCALE = f"{GROK_BASE}/rest/media/video/upscale"
ENDPOINT_POST_FOLDERS = f"{GROK_BASE}/rest/media/post/folders"

MAX_RETRIES = 3
RETRY_DELAY_BASE = 10  # seconds
MAX_RELOGIN_ATTEMPTS = 2


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 80:
        return value
    return f"{value[:60]}...({len(value)} chars)"


# ── Config ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VideoGenConfig:
    aspect_ratio: str = "9:16"
    video_length_seconds: int = 6
    resolution_name: str = "480p"

    def as_dict(self) -> dict[str, Any]:
        resolution = str(self.resolution_name or "480p").strip().lower()
        if resolution not in {"480p", "720p", "1080p"}:
            resolution = "480p"
        return {
            "aspectRatio": self.aspect_ratio,
            "videoLength": int(self.video_length_seconds),
            "isVideoEdit": False,
            "resolutionName": resolution,
        }


# ── Session Data ───────────────────────────────────────────────────────
@dataclass
class GrokSession:
    """Holds captured auth data from browser."""
    email: str = ""
    acc_idx: int = 0
    captured_headers: dict = field(default_factory=dict)
    cookies: list[dict] = field(default_factory=list)
    statsig_id: str = ""
    timestamp: float = 0.0

    @property
    def cookie_str(self) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies if 'name' in c and 'value' in c)

    def build_headers(self, referer: str | None = None) -> dict:
        """Build HTTP request headers from captured browser headers."""
        headers = {}
        for k, v in self.captured_headers.items():
            if not k.startswith(":"):
                headers[k] = v
        headers["content-type"] = "application/json"
        headers["x-xai-request-id"] = str(uuid.uuid4())
        headers["cookie"] = self.cookie_str
        if referer:
            headers["referer"] = referer
        # Remove unwanted headers
        for key in ("host", "content-length"):
            headers.pop(key, None)
        return headers

    def build_download_headers(self) -> dict:
        """Build headers for downloading video files."""
        headers = {}
        for k, v in self.captured_headers.items():
            if not k.startswith(":"):
                headers[k] = v
        headers["cookie"] = self.cookie_str
        headers["referer"] = f"{GROK_BASE}/"
        headers["origin"] = GROK_BASE
        headers["accept"] = "*/*"
        for key in ("host", "content-length", "content-type"):
            headers.pop(key, None)
        return headers


# ── Header Capture from Browser ────────────────────────────────────────
async def capture_session_from_page(page, email: str = "", acc_idx: int = 0) -> GrokSession | None:
    """
    Navigate to grok.com and capture headers + cookies from the browser page.
    This replaces the old auto_discover_statsig_headers (which only got x-statsig-id).
    Now captures ALL request headers for full session authentication.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    captured_headers: dict | None = None

    def on_request(req):
        nonlocal captured_headers
        try:
            h = req.headers or {}
            if not isinstance(h, dict):
                return
            statsig = h.get("x-statsig-id")
            if statsig and captured_headers is None:
                captured_headers = dict(h)
                if not future.done():
                    future.set_result(captured_headers)
        except Exception:
            pass

    page.on("request", on_request)
    try:
        # Navigate to grok.com to trigger requests with auth headers
        try:
            await page.goto(f"{GROK_BASE}/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # Wait for headers
        try:
            await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError:
            pass

        # Retry if not captured
        if captured_headers is None:
            print("🔄 Retry header capture — reloading grok.com...")
            retry_future = loop.create_future()

            def on_request_retry(req):
                nonlocal captured_headers
                try:
                    h = req.headers or {}
                    if not isinstance(h, dict):
                        return
                    statsig = h.get("x-statsig-id")
                    if statsig and captured_headers is None:
                        captured_headers = dict(h)
                        if not retry_future.done():
                            retry_future.set_result(captured_headers)
                except Exception:
                    pass

            page.on("request", on_request_retry)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            try:
                await asyncio.wait_for(retry_future, timeout=15)
            except asyncio.TimeoutError:
                pass
            try:
                page.remove_listener("request", on_request_retry)
            except Exception:
                pass

        if captured_headers is None:
            print("❌ Failed to capture headers")
            return None

        # Extract cookies
        browser_context = page.context
        cookies = await browser_context.cookies(f"{GROK_BASE}")
        print(f"✅ Captured {len(captured_headers)} headers, {len(cookies)} cookies")

        session = GrokSession(
            email=email,
            acc_idx=acc_idx,
            captured_headers=captured_headers,
            cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
            statsig_id=captured_headers.get("x-statsig-id", ""),
            timestamp=asyncio.get_event_loop().time(),
        )
        return session

    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass


# ── Compatibility wrapper ──────────────────────────────────────────────
async def auto_discover_statsig_headers(
    page,
    cache_path: Path,
    profile_name: str,
    force: bool = False,
    persist: bool = False,
) -> dict:
    """
    Backward-compatible wrapper. Returns headers dict like the old API,
    but internally captures the full session.
    """
    session = await capture_session_from_page(page)
    if session is None:
        return {}
    return session.captured_headers


# ── API: Create Post ───────────────────────────────────────────────────
async def create_post(prompt: str, session: GrokSession) -> dict:
    """Create a media post (required before video generation)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                ENDPOINT_CREATE_POST,
                json={"mediaType": "MEDIA_POST_TYPE_VIDEO", "prompt": prompt},
                headers=session.build_headers(),
            )
        if res.status_code != 200:
            return {
                "error": f"createPost HTTP {res.status_code}",
                "errorDetail": res.text[:500],
            }
        data = res.json()
        post_id = data.get("post", {}).get("id")
        if not post_id:
            return {"error": "no postId returned"}
        return {"postId": post_id}
    except Exception as e:
        return {"error": str(e)}


# ── API: Generate Video (streaming) ───────────────────────────────────
def _build_video_body(prompt: str, parent_post_id: str, cfg: VideoGenConfig) -> dict:
    """Build the conversation/new request body for video generation."""
    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": f"{prompt} --mode=custom",
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {
                "modelMap": {
                    "videoGenModelConfig": {
                        "parentPostId": parent_post_id,
                        **cfg.as_dict(),
                    }
                }
            },
        },
    }


def _parse_stream_line(line: str, result: dict) -> None:
    """Parse one NDJSON line from the streaming response."""
    if not line.strip():
        return
    try:
        j = json.loads(line)
    except json.JSONDecodeError:
        return

    # Title
    if j.get("result", {}).get("title", {}).get("newTitle"):
        result["title"] = j["result"]["title"]["newTitle"]

    # Errors
    for err_source in [
        j.get("error"),
        j.get("result", {}).get("error"),
        j.get("result", {}).get("response", {}).get("modelResponse", {}).get("error"),
    ]:
        if err_source and not result.get("error"):
            msg = err_source if isinstance(err_source, str) else (
                err_source.get("message", json.dumps(err_source)) if isinstance(err_source, dict) else str(err_source)
            )
            result["error"] = msg

    # Content blocking
    mr = j.get("result", {}).get("response", {}).get("modelResponse", {})
    if (mr.get("isSoftBlock") or mr.get("isDisallowed")) and not result.get("error"):
        result["error"] = f"Content blocked: softBlock={mr.get('isSoftBlock')}, disallowed={mr.get('isDisallowed')}"

    # Video progress
    vr = j.get("result", {}).get("response", {}).get("streamingVideoGenerationResponse")
    if vr:
        result["progress"] = vr.get("progress", result.get("progress", 0))
        if vr.get("videoId"):
            result["videoId"] = vr["videoId"]
        if vr.get("assetId") and not result.get("videoId"):
            result["videoId"] = vr["assetId"]
        if vr.get("videoUrl"):
            result["videoUrl"] = vr["videoUrl"]
        if vr.get("parentPostId"):
            result["parentPostId_from_stream"] = vr["parentPostId"]
        if vr.get("error") and not result.get("error"):
            err = vr["error"]
            result["error"] = err if isinstance(err, str) else str(err)


async def generate_video(
    prompt: str,
    session: GrokSession,
    cfg: VideoGenConfig,
    on_progress: Callable[[int, int], None] | None = None,
    job_index: int = 0,
) -> dict:
    """
    Generate a single video via direct HTTP.
    Steps: create post → stream conversation → parse result.
    Retries on 429 (rate limit) and network errors.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            # Step 1: Create post
            suffix = f" (retry {attempt}/{MAX_RETRIES})" if attempt > 0 else ""
            print(f"[VideoAPI] Creating post: {prompt[:50]}...{suffix}")

            post = await create_post(prompt, session)
            if post.get("error"):
                if "HTTP 403" in str(post.get("error", "")) and attempt < MAX_RETRIES:
                    print(f"[VideoAPI] ⚠️ 403 from createPost, will retry...")
                    await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                    continue
                return {
                    "prompt": prompt,
                    "title": "",
                    "videoUrl": None,
                    "videoId": None,
                    "progress": 0,
                    "error": post.get("error"),
                    "parentPostId": None,
                    "createStatus": 0,
                    "convoStatus": 0,
                }

            parent_post_id = post["postId"]
            print(f"[VideoAPI] Post created: {parent_post_id}")

            # Step 2: Generate video (streaming)
            result = {
                "prompt": prompt,
                "title": "",
                "videoUrl": None,
                "videoId": None,
                "progress": 0,
                "error": None,
                "parentPostId": parent_post_id,
                "createStatus": 200,
                "convoStatus": 0,
            }

            body = _build_video_body(prompt, parent_post_id, cfg)
            headers = session.build_headers()

            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
                async with client.stream(
                    "POST",
                    ENDPOINT_CONVO_NEW,
                    json=body,
                    headers=headers,
                ) as response:
                    result["convoStatus"] = response.status_code

                    # Handle error status codes
                    if response.status_code == 429 and attempt < MAX_RETRIES:
                        wait = RETRY_DELAY_BASE * (attempt + 1) + 5
                        print(f"[VideoAPI] ⚠️ Rate limited (429), retry in {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    if response.status_code == 403 and attempt < MAX_RETRIES:
                        print(f"[VideoAPI] ⚠️ 403 Forbidden, retry...")
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

                    # Parse streaming NDJSON response
                    buffer = ""
                    last_reported = 0
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        lines = buffer.split("\n")
                        buffer = lines.pop()  # keep incomplete line

                        for line in lines:
                            _parse_stream_line(line, result)
                            pct = int(result.get("progress", 0))
                            if pct > last_reported and on_progress:
                                on_progress(job_index, pct)
                                last_reported = pct

                    # Process remaining buffer
                    if buffer.strip():
                        _parse_stream_line(buffer, result)

            # Fallback: use videoId as download key
            if not result.get("videoUrl") and result.get("videoId"):
                result["videoUrl"] = result["videoId"]
                print(f"[VideoAPI] Using videoId as download key: {result['videoId']}")

            if not result.get("videoUrl") and not result.get("error"):
                result["error"] = f"Video gen stopped at {result.get('progress', 0)}% - no video URL"

            return result

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY_BASE * (attempt + 1)
                print(f"[VideoAPI] ⚠️ Network error: {e}, retry in {wait}s...")
                await asyncio.sleep(wait)
                continue
            return {
                "prompt": prompt,
                "title": "",
                "videoUrl": None,
                "videoId": None,
                "progress": 0,
                "error": str(e),
                "parentPostId": None,
                "createStatus": 0,
                "convoStatus": 0,
            }


# ── API: Upscale Video ────────────────────────────────────────────────
async def upscale_video(video_id: str, session: GrokSession, max_retries: int = 3) -> str | None:
    """Request HD upscale for a video. Returns hdMediaUrl or None."""
    vid = (video_id or "").strip()
    if not vid:
        return None

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.post(
                    ENDPOINT_UPSCALE,
                    json={"videoId": vid},
                    headers=session.build_headers(),
                )
            if res.status_code == 200:
                data = res.json()
                hd_url = data.get("hdMediaUrl")
                if hd_url:
                    return str(hd_url)
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)
        except Exception:
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)
    return None


# ── API: Download Video ───────────────────────────────────────────────
async def download_video(
    video_url: str,
    session: GrokSession,
    out_path: Path,
    timeout_seconds: int = 180,
    max_attempts: int = 15,
    retry_delay: int = 5,
) -> bool:
    """Download a video file to disk. Returns True on success.
    Uses aggressive polling (15 attempts × 5s) matching AutoGrok's downloadVideo()
    because video may not be ready immediately after stream ends.
    """
    target = (video_url or "").strip()
    if not target:
        return False

    # Build full URL if needed
    if not target.startswith("http"):
        target = f"{ASSETS_BASE}{target}"

    headers = session.build_download_headers()

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"⬇️ Downloading ({attempt}/{max_attempts}): {target[:80]}...")
            async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
                res = await client.get(target, headers=headers)

            if res.status_code == 200 and len(res.content) > 1000:
                # Verify it looks like an MP4
                is_mp4 = len(res.content) > 12 and res.content[4:8] == b"ftyp"
                ct = (res.headers.get("content-type") or "").lower()
                if is_mp4 or "video" in ct or "octet-stream" in ct:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(res.content)
                    size_mb = len(res.content) / (1024 * 1024)
                    print(f"⬇️ Saved: {out_path} ({size_mb:.1f} MB)")
                    return True

            if res.status_code in (404, 403) and attempt < max_attempts:
                print(f"⏳ Video not ready ({res.status_code}), retry {attempt}/{max_attempts}...")
                await asyncio.sleep(retry_delay)
                continue

            print(f"⚠️ Download HTTP {res.status_code} ({len(res.content)} bytes)")
        except Exception as e:
            print(f"⚠️ Download error: {str(e)[:60]}")
            if attempt < max_attempts:
                await asyncio.sleep(retry_delay)
                continue
    return False


# ── Batch Processing ──────────────────────────────────────────────────
async def run_batch_text_to_video(
    prompts: list[str],
    session: GrokSession,
    cfg: VideoGenConfig,
    concurrency: int = 5,
    download_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_status: Callable[[int, str], None] | None = None,
    on_video: Callable[[int, str], None] | None = None,
    on_info: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    Run multiple video generations concurrently.
    Uses worker pool pattern from AutoGrok.
    """
    N = len(prompts)
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict] = [None] * N  # type: ignore

    def _safe(cb, *args):
        try:
            if cb:
                cb(*args)
        except Exception:
            pass

    async def process_one(idx: int, prompt: str):
        async with sem:
            _safe(on_status, idx, "Tạo post")
            _safe(on_info, f"[GROK-T2V {idx+1}] bắt đầu job")

            result = await generate_video(prompt, session, cfg, on_progress, idx)

            # Handle 403 - try refreshing session won't work in this context
            # The caller should handle re-auth
            convo_status = result.get("convoStatus", 0)
            if convo_status == 403:
                _safe(on_status, idx, "Lỗi 403")
                _safe(on_info, f"[GROK-T2V {idx+1}] lỗi 403 - cần login lại")
                results[idx] = result
                return

            if result.get("error") and not result.get("videoUrl"):
                _safe(on_status, idx, "Lỗi")
                _safe(on_info, f"[GROK-T2V {idx+1}] lỗi: {result.get('error', '')[:80]}")
                results[idx] = result
                return

            # Update progress
            pct = int(result.get("progress", 0))
            if pct >= 100:
                _safe(on_status, idx, "Tạo xong")
                _safe(on_progress, idx, 100)

            # Upscale if not 720p
            parent_id = result.get("parentPostId", "")
            is_720p = str(cfg.resolution_name or "").lower() == "720p"
            hd_url = None

            if pct >= 100 and parent_id and not is_720p:
                _safe(on_status, idx, "Đang upscale")
                _safe(on_info, f"[GROK-T2V {idx+1}] upscale...")
                hd_url = await upscale_video(parent_id, session)
                if hd_url:
                    result["hdMediaUrl"] = hd_url
                    result["usedUpscale"] = True

            # Download video
            source_url = hd_url or result.get("videoUrl")
            if not source_url and parent_id:
                # Fallback: construct public download URL
                source_url = (
                    f"https://imagine-public.x.ai/imagine-public/share-videos/"
                    f"{parent_id}.mp4?cache=1&dl=1"
                )

            if source_url and download_dir:
                _safe(on_status, idx, "Tải video")
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                pr_short = re.sub(r"[^A-Za-z0-9._-]+", "_", (prompt[:40] or "")).strip("._- ")
                filename = f"{idx+1:03d}_{pr_short}_{ts}.mp4"
                out_path = download_dir / filename

                ok = await download_video(source_url, session, out_path)
                if ok:
                    result["savedFile"] = str(out_path)
                    _safe(on_video, idx, str(out_path))
                    _safe(on_status, idx, "Hoàn thành")
                    _safe(on_info, f"[GROK-T2V {idx+1}] hoàn thành")
                else:
                    # Retry with upscale
                    if parent_id:
                        fresh_hd = await upscale_video(parent_id, session)
                        if fresh_hd:
                            ok2 = await download_video(fresh_hd, session, out_path)
                            if ok2:
                                result["savedFile"] = str(out_path)
                                _safe(on_video, idx, str(out_path))
                                _safe(on_status, idx, "Hoàn thành")
                                _safe(on_info, f"[GROK-T2V {idx+1}] hoàn thành sau retry")
                                results[idx] = result
                                return
                    _safe(on_status, idx, "Lỗi tải")
                    _safe(on_info, f"[GROK-T2V {idx+1}] tải thất bại")
            else:
                if convo_status == 200 and pct >= 100:
                    _safe(on_status, idx, "Hoàn thành")
                else:
                    _safe(on_status, idx, f"Lỗi {convo_status}")

            results[idx] = result

    tasks = [asyncio.create_task(process_one(i, p)) for i, p in enumerate(prompts)]
    await asyncio.gather(*tasks, return_exceptions=True)

    return [r or {"error": "task failed"} for r in results]


# ── Legacy compatibility functions ─────────────────────────────────────
# These maintain backward compatibility with existing code that uses them


def payload_create_post(prompt: str) -> dict[str, Any]:
    return {"mediaType": "MEDIA_POST_TYPE_VIDEO", "prompt": prompt}


def payload_conversation_new(prompt: str, parent_post_id: str, cfg: VideoGenConfig) -> dict[str, Any]:
    return _build_video_body(prompt, parent_post_id, cfg)


def payload_upscale(video_id: str) -> dict[str, Any]:
    return {"videoId": video_id}


# ── Legacy cache functions (still used by some callers) ────────────────
def _load_cache(cache_path: Path) -> dict:
    try:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_cached_headers(cache_path: Path, profile_name: str) -> dict:
    cache = _load_cache(cache_path)
    profiles = cache.get("profiles") if isinstance(cache, dict) else None
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        return {}
    headers = entry.get("custom_headers")
    if not isinstance(headers, dict):
        return {}
    headers = dict(headers)
    headers.pop("x-xai-request-id", None)
    return headers


def set_cached_headers(cache_path: Path, profile_name: str, headers: dict) -> None:
    headers = dict(headers or {})
    headers.pop("x-xai-request-id", None)
    cache = _load_cache(cache_path)
    if not isinstance(cache, dict):
        cache = {}
    profiles = cache.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        cache["profiles"] = profiles
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        entry = {}
    entry["custom_headers"] = headers
    entry["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    profiles[profile_name] = entry
    _save_cache(cache_path, cache)


def profile_cache_age_seconds(cache_path: Path, profile_name: str) -> float | None:
    cache = _load_cache(cache_path)
    profiles = cache.get("profiles") if isinstance(cache, dict) else None
    if not isinstance(profiles, dict):
        return None
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        return None
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(updated_at.strip())
        return (datetime.datetime.now() - dt).total_seconds()
    except Exception:
        return None


# ── Legacy page.evaluate functions (kept for backward compat) ──────────
# These are no longer the primary path but kept so old callers don't break

async def api_run_single_job_in_page(
    page, prompt: str, statsig_headers: dict, cfg: VideoGenConfig,
    timeout_seconds: int, job_index: int,
) -> dict:
    """Legacy: now wraps direct HTTP call instead of page.evaluate()."""
    # Build a minimal session from statsig_headers + page cookies
    browser_context = page.context
    cookies = await browser_context.cookies(GROK_BASE)
    session = GrokSession(
        captured_headers=statsig_headers,
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
        statsig_id=statsig_headers.get("x-statsig-id", ""),
    )
    result = await generate_video(prompt, session, cfg, job_index=job_index)
    return result


async def api_run_jobs_in_page(
    page, prompts: list[str], statsig_headers: dict, cfg: VideoGenConfig,
    timeout_seconds: int, index_offset: int = 0,
) -> list[dict]:
    """Legacy: now wraps direct HTTP batch call instead of page.evaluate()."""
    browser_context = page.context
    cookies = await browser_context.cookies(GROK_BASE)
    session = GrokSession(
        captured_headers=statsig_headers,
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
        statsig_id=statsig_headers.get("x-statsig-id", ""),
    )

    results = []
    for i, prompt in enumerate(prompts):
        result = await generate_video(prompt, session, cfg, job_index=index_offset + i)
        results.append(result)
    return results


async def download_mp4(context, url: str, out_path: Path, timeout_ms: int) -> bool:
    """Legacy download function - now uses httpx instead of Playwright context."""
    # Create a minimal session with empty headers (downloads may not need auth)
    cookies = await context.cookies(GROK_BASE)
    session = GrokSession(
        cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
    )
    return await download_video(url, session, out_path, timeout_seconds=timeout_ms // 1000)
