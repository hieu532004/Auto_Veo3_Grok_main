"""
Grok Text-to-Video Workflow — 1:1 port of AutoGrok's T2V flow.

Flow:
  1. Open Chrome, navigate to grok.com
  2. capture_session_from_page() → GrokSession (headers + cookies)
  3. Close browser
  4. Run batch generation via direct HTTP `run_batch_text_to_video` from the API.

This module provides the synchronous entry point `run_text_to_video_jobs` for the QThread worker.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import re
import threading
from pathlib import Path
from typing import Callable

from grok_api_text_to_video import (
    GrokSession,
    VideoGenConfig,
    capture_session_from_page,
    run_batch_text_to_video,
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
VideoCallback = Callable[[int, str], None]
InfoCallback = Callable[[str], None]


def _safe_call(cb, *args) -> None:
    try:
        if cb:
            cb(*args)
    except Exception:
        pass


async def _run_jobs_async_ui(
    prompts: list[str],
    cfg: VideoGenConfig,
    max_concurrency: int,
    on_status: StatusCallback | None,
    on_progress: ProgressCallback | None,
    on_video: VideoCallback | None,
    on_info: InfoCallback | None,
    stop_event: threading.Event | None = None,
    offscreen_chrome: bool = False,
    download_dir: Path | None = None,
) -> None:
    """Run text-to-video using direct HTTP batch API."""

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
        host=CDP_HOST,
        port=CDP_PORT,
        user_data_dir=USER_DATA_DIR,
        start_url="https://grok.com/",
        cdp_wait_seconds=30,
        offscreen=bool(offscreen_chrome),
    )

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
            for i in range(len(prompts)):
                _safe_call(on_status, i, "Lỗi session")
            return

        _safe_call(on_info, f"Session ready: {len(grok_session.captured_headers)} headers, {len(grok_session.cookies)} cookies")
        
        # Close browser immediately since everything is direct HTTP now.
        await session.close()

        if _stop_requested():
            return

        # Prepare for generation API
        out_dir = download_dir or (DOWNLOAD_DIR / "grok_video")
        out_dir.mkdir(parents=True, exist_ok=True)

        for i in range(len(prompts)):
            _safe_call(on_status, i, "Đang chờ...")
            _safe_call(on_progress, i, 0)

        def _on_progress(idx: int, pct: int):
            if _stop_requested():
                raise asyncio.CancelledError("Stop requested")
            _safe_call(on_progress, idx, pct)

        # Run batch via HTTP
        results = await run_batch_text_to_video(
            prompts=prompts,
            session=grok_session,
            cfg=cfg,
            concurrency=max_concurrency,
            download_dir=out_dir,
            on_progress=_on_progress,
            on_status=on_status,
            on_video=on_video,
            on_info=on_info,
        )

        ok_count = sum(1 for r in results if r and r.get("savedFile"))
        _safe_call(on_info, f"Batch complete: {ok_count}/{len(results)} successful")

    except asyncio.CancelledError:
        _safe_call(on_info, "Job cancelled by user.")
    finally:
        try:
            await session.close()
        except Exception:
            pass


def run_text_to_video_jobs(
    prompts: list[str],
    aspect_ratio: str,
    video_length_seconds: int,
    resolution_name: str,
    max_concurrency: int,
    download_dir: str | None = None,
    offscreen_chrome: bool = False,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_video: VideoCallback | None = None,
    on_info: InfoCallback | None = None,
) -> None:
    """Synchronous entry point for QThread to run batch text-to-video jobs."""
    global DOWNLOAD_DIR
    cleaned_prompts = [p.strip() for p in (prompts or []) if isinstance(p, str) and p.strip()]
    if not cleaned_prompts:
        raise ValueError("Danh sách prompt rỗng.")

    if isinstance(download_dir, str) and download_dir.strip():
        dl_path = Path(download_dir.strip()) / "grok_video"
    else:
        dl_path = Path(DOWNLOAD_DIR) / "grok_video"
    dl_path.mkdir(parents=True, exist_ok=True)

    runtime_resolution = str(resolution_name or "480p")
    if runtime_resolution not in {"480p", "720p"}:
        runtime_resolution = "480p"

    runtime_aspect = str(aspect_ratio or "9:16").strip()
    if runtime_aspect not in {"9:16", "16:9"}:
        runtime_aspect = "9:16"

    cfg = VideoGenConfig(
        aspect_ratio=runtime_aspect,
        video_length_seconds=int(video_length_seconds),
        resolution_name=runtime_resolution,
    )
    
    asyncio.run(
        _run_jobs_async_ui(
            prompts=cleaned_prompts,
            cfg=cfg,
            max_concurrency=max_concurrency,
            on_status=on_status,
            on_progress=on_progress,
            on_video=on_video,
            on_info=on_info,
            stop_event=stop_event,
            offscreen_chrome=bool(offscreen_chrome),
            download_dir=dl_path,
        )
    )
