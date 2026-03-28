"""
Grok Create Image Workflow — 1:1 port of AutoGrok's flow.

Flow:
  1. Open Chrome, navigate to grok.com
  2. capture_session_from_page() → GrokSession (headers + cookies)
  3. Close browser (AutoGrok pattern: browser only for auth)
  4. Run batch generation via direct HTTP (ImageService or RefImageService)
  5. On 403 → re-capture session from page → retry

This module provides synchronous entry points for the QThread workers.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Callable

from grok_api_create_image import (
    generate_batch_images,
    generate_batch_ref_images,
    MAX_RELOGIN_ATTEMPTS,
)
from grok_api_text_to_video import (
    GrokSession,
    capture_session_from_page,
)
from grok_chrome_manager import open_chrome_session, resolve_profile_dir


CDP_HOST = os.getenv("GROK_CDP_HOST", os.getenv("CDP_HOST", "127.0.0.1"))
CDP_PORT = int(os.getenv("GROK_CDP_PORT", os.getenv("CDP_PORT", "9223")))

WORKSPACE_DIR = Path(__file__).resolve().parent
PROFILE_NAME = os.getenv("GROK_PROFILE_NAME", os.getenv("PROFILE_NAME", "PROFILE_1"))
USER_DATA_DIR = resolve_profile_dir(PROFILE_NAME)

DOWNLOAD_DIR = Path(os.getenv("GROK_DOWNLOAD_DIR", str(WORKSPACE_DIR / "downloads")))

StatusCallback = Callable[[int, str], None]
ProgressCallback = Callable[[int, int], None]
ImageSavedCallback = Callable[[int, str], None]
InfoCallback = Callable[[str], None]


def _safe_call(cb, *args) -> None:
    try:
        if cb:
            cb(*args)
    except Exception:
        pass


async def _run_create_image_async(
    prompts: list[str],
    max_concurrency: int,
    image_count: int,
    aspect_ratio: str,
    on_status: StatusCallback | None,
    on_progress: ProgressCallback | None,
    on_image: ImageSavedCallback | None,
    on_info: InfoCallback | None,
    stop_event: threading.Event | None = None,
    offscreen_chrome: bool = False,
    download_dir: Path | None = None,
) -> None:
    """Run text-to-image — follows AutoGrok flow: browser→capture→close→httpx batch."""

    def _stop_requested() -> bool:
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    if _stop_requested():
        _safe_call(on_info, "Stop requested before start")
        return

    _safe_call(on_info, "Opening Chrome for session capture...")

    session = await open_chrome_session(
        host=CDP_HOST, port=CDP_PORT,
        user_data_dir=USER_DATA_DIR,
        start_url="https://grok.com/",
        cdp_wait_seconds=30,
        offscreen=bool(offscreen_chrome),
    )

    # We keep session open for potential re-login capture
    relogin_count = [0]
    page_ref = [None]

    try:
        if _stop_requested():
            return

        context = session.context
        page = None
        for candidate in list(context.pages):
            try:
                if not candidate.is_closed() and "grok.com" in (candidate.url or ""):
                    page = candidate
                    break
            except Exception:
                continue
        if page is None:
            page = await context.new_page()

        try:
            await page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        if _stop_requested():
            return

        # Capture session
        _safe_call(on_info, "Capturing session (headers + cookies)...")
        grok_session = await capture_session_from_page(page)
        if grok_session is None:
            _safe_call(on_info, "Failed to capture session")
            return

        page_ref[0] = page
        _safe_call(on_info, f"Session ready: {len(grok_session.captured_headers)} headers, {len(grok_session.cookies)} cookies")

        if _stop_requested():
            return

        out_dir = download_dir or (DOWNLOAD_DIR / "grok_image")
        out_dir.mkdir(parents=True, exist_ok=True)

        # Re-login function (called on 403 from within API)
        async def relogin_fn() -> GrokSession | None:
            if relogin_count[0] >= MAX_RELOGIN_ATTEMPTS:
                _safe_call(on_info, f"Max re-login attempts ({MAX_RELOGIN_ATTEMPTS}) exceeded")
                return None
            relogin_count[0] += 1
            _safe_call(on_info, f"Re-capturing session (attempt {relogin_count[0]}/{MAX_RELOGIN_ATTEMPTS})...")
            p = page_ref[0]
            if p is None or p.is_closed():
                return None
            try:
                await p.goto("https://grok.com/", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            new_session = await capture_session_from_page(p)
            if new_session:
                _safe_call(on_info, "Re-login OK")
            else:
                _safe_call(on_info, "Re-login failed")
            return new_session

        # Build config
        cfg = {
            "aspectRatio": aspect_ratio,
            "imageGenerationCount": image_count,
            "batchSize": max_concurrency,
        }

        # Progress adapter
        def on_prog(prompt, progress, job_result, local_idx):
            if _stop_requested():
                return
            _safe_call(on_progress, local_idx, int(progress))
            if job_result:
                if job_result.get("success"):
                    _safe_call(on_status, local_idx, "Hoàn thành")
                    for fp in job_result.get("savedFiles", []):
                        _safe_call(on_image, local_idx, fp)
                else:
                    err = job_result.get("error", "unknown")
                    _safe_call(on_status, local_idx, f"Lỗi: {str(err)[:50]}")

        # Set initial status
        for i in range(len(prompts)):
            _safe_call(on_status, i, "Đang chờ...")

        results = await generate_batch_images(
            prompts=prompts,
            session=grok_session,
            config=cfg,
            on_progress=on_prog,
            start_idx=0,
            output_folder=out_dir,
            relogin_fn=relogin_fn,
        )

        # Summary
        ok = sum(1 for r in results if r.get("success"))
        _safe_call(on_info, f"Done: {ok}/{len(results)} successful")

    finally:
        await session.close()


async def _run_create_image_reference_async(
    items: list[dict],
    max_concurrency: int,
    image_count: int,
    on_status: StatusCallback | None,
    on_progress: ProgressCallback | None,
    on_image: ImageSavedCallback | None,
    on_info: InfoCallback | None,
    stop_event: threading.Event | None = None,
    offscreen_chrome: bool = False,
    download_dir: Path | None = None,
) -> None:
    """Run reference image generation — follows AutoGrok RefImageService flow."""

    def _stop_requested() -> bool:
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    if _stop_requested():
        return

    _safe_call(on_info, "Opening Chrome for session capture...")

    session = await open_chrome_session(
        host=CDP_HOST, port=CDP_PORT,
        user_data_dir=USER_DATA_DIR,
        start_url="https://grok.com/",
        cdp_wait_seconds=30,
        offscreen=bool(offscreen_chrome),
    )

    relogin_count = [0]
    page_ref = [None]

    try:
        if _stop_requested():
            return

        context = session.context
        page = None
        for candidate in list(context.pages):
            try:
                if not candidate.is_closed() and "grok.com" in (candidate.url or ""):
                    page = candidate
                    break
            except Exception:
                continue
        if page is None:
            page = await context.new_page()

        try:
            await page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        if _stop_requested():
            return

        _safe_call(on_info, "Capturing session (headers + cookies)...")
        grok_session = await capture_session_from_page(page)
        if grok_session is None:
            _safe_call(on_info, "Failed to capture session")
            return

        page_ref[0] = page
        _safe_call(on_info, f"Session ready: {len(grok_session.captured_headers)} headers")

        if _stop_requested():
            return

        out_dir = download_dir or (DOWNLOAD_DIR / "grok_image")
        out_dir.mkdir(parents=True, exist_ok=True)

        async def relogin_fn() -> GrokSession | None:
            if relogin_count[0] >= MAX_RELOGIN_ATTEMPTS:
                return None
            relogin_count[0] += 1
            _safe_call(on_info, f"Re-capturing session ({relogin_count[0]}/{MAX_RELOGIN_ATTEMPTS})...")
            p = page_ref[0]
            if p is None or p.is_closed():
                return None
            try:
                await p.goto("https://grok.com/", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            return await capture_session_from_page(p)

        cfg = {"imageGenerationCount": image_count, "batchSize": max_concurrency}

        def on_prog(prompt, progress, job_result, local_idx):
            if _stop_requested():
                return
            _safe_call(on_progress, local_idx, int(progress))
            if job_result:
                if job_result.get("success"):
                    _safe_call(on_status, local_idx, "Hoàn thành")
                    for fp in job_result.get("savedFiles", []):
                        _safe_call(on_image, local_idx, fp)
                else:
                    _safe_call(on_status, local_idx, f"Lỗi: {str(job_result.get('error', ''))[:50]}")

        for i in range(len(items)):
            _safe_call(on_status, i, "Đang chờ...")

        results = await generate_batch_ref_images(
            items=items,
            session=grok_session,
            config=cfg,
            on_progress=on_prog,
            start_idx=0,
            output_folder=out_dir,
            relogin_fn=relogin_fn,
        )

        ok = sum(1 for r in results if r.get("success"))
        _safe_call(on_info, f"Done: {ok}/{len(results)} successful")

    finally:
        await session.close()


# ── Public sync entry points ──────────────────────────────────────────
def run_grok_create_image_jobs(
    prompts: list[str],
    max_concurrency: int = 30,
    image_count: int = 2,
    aspect_ratio: str = "1:1",
    download_dir: str | None = None,
    offscreen_chrome: bool = False,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_image: ImageSavedCallback | None = None,
    on_info: InfoCallback | None = None,
) -> None:
    """Run Grok text-to-image generation (synchronous wrapper for QThread)."""
    cleaned = [p.strip() for p in (prompts or []) if isinstance(p, str) and p.strip()]
    if not cleaned:
        raise ValueError("Danh sách prompt rỗng.")

    dl_dir = Path(download_dir.strip()) / "grok_image" if isinstance(download_dir, str) and download_dir.strip() else DOWNLOAD_DIR / "grok_image"
    dl_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_run_create_image_async(
        prompts=cleaned,
        max_concurrency=max_concurrency,
        image_count=image_count,
        aspect_ratio=aspect_ratio,
        on_status=on_status,
        on_progress=on_progress,
        on_image=on_image,
        on_info=on_info,
        stop_event=stop_event,
        offscreen_chrome=bool(offscreen_chrome),
        download_dir=dl_dir,
    ))


def run_grok_create_image_reference_jobs(
    items: list[dict],
    max_concurrency: int = 5,
    image_count: int = 2,
    download_dir: str | None = None,
    offscreen_chrome: bool = False,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_image: ImageSavedCallback | None = None,
    on_info: InfoCallback | None = None,
) -> None:
    """Run Grok reference-image generation (synchronous wrapper for QThread)."""
    clean_items = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        paths = raw.get("refImagePaths") or raw.get("image_paths") or []
        if isinstance(paths, str):
            paths = [paths]
        # Also support single image_path
        if not paths and raw.get("image_path"):
            paths = [str(raw["image_path"])]
        if not paths and raw.get("path"):
            paths = [str(raw["path"])]
        paths = [str(p).strip() for p in paths if str(p).strip()]
        if not paths:
            continue
        clean_items.append({
            "prompt": str(raw.get("prompt") or raw.get("description") or "").strip(),
            "refImagePaths": paths,
        })

    if not clean_items:
        raise ValueError("Danh sách reference image rỗng.")

    dl_dir = Path(download_dir.strip()) / "grok_image" if isinstance(download_dir, str) and download_dir.strip() else DOWNLOAD_DIR / "grok_image"
    dl_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_run_create_image_reference_async(
        items=clean_items,
        max_concurrency=max_concurrency,
        image_count=image_count,
        on_status=on_status,
        on_progress=on_progress,
        on_image=on_image,
        on_info=on_info,
        stop_event=stop_event,
        offscreen_chrome=bool(offscreen_chrome),
        download_dir=dl_dir,
    ))
