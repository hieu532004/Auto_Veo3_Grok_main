import os
import subprocess
import shutil
from pathlib import Path


FIFE_RESOLUTION_MAP = {
    "720":  "=w1280-h720-p",
    "1080": "=w1920-h1080-p",
    "2K":   "=w2560-h1440-p",
    "4K":   "=w3840-h2160-p",
}

FIFE_HOSTS = [
    "lh3.googleusercontent.com",
    "lh4.googleusercontent.com",
    "lh5.googleusercontent.com",
    "lh6.googleusercontent.com",
    "video.googleusercontent.com",
    "play-lh.googleusercontent.com",
]

STORAGE_HOSTS = [
    "storage.googleapis.com",
    "storage.cloud.google.com",
]


def is_fife_url(url: str) -> bool:
    return any(host in url for host in FIFE_HOSTS)


def is_storage_url(url: str) -> bool:
    return any(host in url for host in STORAGE_HOSTS)


def apply_download_resolution(url: str, download_mode: str) -> str:
    if not url or not download_mode:
        return url

    mode = str(download_mode).strip()

    if is_fife_url(url):
        clean = url.split("=")[0]
        suffix = FIFE_RESOLUTION_MAP.get(mode, "=d")
        return clean + suffix

    return url


def _get_ffmpeg_path():
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None


def _get_video_dimensions(ffmpeg_path, video_path):
    try:
        cmd = [
            ffmpeg_path, "-i", video_path,
            "-hide_banner",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = result.stderr or ""
        import re
        m = re.search(r"(\d{2,5})x(\d{2,5})", output)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None, None


def get_target_resolution(w, h, dl_mode):
    is_portrait = h > w
    if dl_mode == "1080":
        return (1080, 1920) if is_portrait else (1920, 1080)
    elif dl_mode == "2K":
        return (1440, 2560) if is_portrait else (2560, 1440)
    elif dl_mode == "4K":
        return (2160, 3840) if is_portrait else (3840, 2160)
    else: 
        return (720, 1280) if is_portrait else (1280, 720)


def remove_watermark(video_path, log_callback=None):
    if not video_path or not os.path.isfile(video_path):
        return video_path

    ffmpeg_path = _get_ffmpeg_path()
    if not ffmpeg_path:
        if log_callback:
            log_callback("⚠️  Không tìm thấy ffmpeg, bỏ qua xử lý video")
        return video_path

    w, h = _get_video_dimensions(ffmpeg_path, video_path)
    if not w or not h:
        if log_callback:
            log_callback("⚠️  Không đọc được kích thước, giữ tệp gốc")
        return video_path

    from settings_manager import SettingsManager
    try:
        config = SettingsManager.load_config()
        dl_mode = str(config.get("DOWNLOAD_MODE", "720") or "720").strip()
    except Exception:
        dl_mode = "720"

    target_w, target_h = get_target_resolution(w, h, dl_mode)

    # Crop 12% để cắt đứt hẳn logo ở góc dưới bên phải
    # Crop top-left (center theo chiều ngang, và sát mép trên)
    crop_w = int(w * 0.88)
    crop_h = int(h * 0.88)
    
    # Cân giữa theo chiều ngang (cắt đều 2 bên góc trái/phải)
    crop_x = (w - crop_w) // 2 
    # Cắt toàn bộ phần dưới cùng (logo Veo nằm ở mép dưới cùng)
    crop_y = 0

    p = Path(video_path)
    temp_path = str(p.parent / f"{p.stem}_out{p.suffix}")

    cmd = [
        ffmpeg_path,
        "-y",
        "-i", video_path,
        "-vf", f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={target_w}:{target_h}",
        "-c:a", "copy",
        "-preset", "fast",
        temp_path,
    ]

    try:
        if log_callback:
            log_callback(f"🔄  Đang xoá logo hoàn toàn và xuất ra định dạng {dl_mode} ({target_w}x{target_h})...")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and os.path.isfile(temp_path):
            temp_size = os.path.getsize(temp_path)
            if temp_size > 1000:
                os.replace(temp_path, video_path)
                if log_callback:
                    log_callback("✅  Video sẵn sàng (đã xóa logo và cấu hình chuẩn)")
                return video_path
            else:
                os.remove(temp_path)
                if log_callback:
                    log_callback("⚠️  File video xuất ra lỗi (quá nhỏ), giữ file gốc")
        else:
            if log_callback:
                err = (result.stderr or "")[-300:]
                log_callback(f"⚠️  Xử lý thất bại: {err}")
            if os.path.isfile(temp_path):
                os.remove(temp_path)
    except Exception as exc:
        if log_callback:
            log_callback(f"⚠️  Lỗi xử lý video: {exc}")
        if os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return video_path
