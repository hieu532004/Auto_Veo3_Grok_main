from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

_current_lang = "vi"
_listeners: list[Callable[[], None]] = []

_SETTINGS_DIR = Path(__file__).resolve().parent.parent / "data_general"
_LANG_FILE = _SETTINGS_DIR / "app_language.txt"

TRANSLATIONS: dict[str, dict[str, str]] = {
    "app_title_suffix": {"vi": "TOOL VEO_GROK", "en": "VEO_GROK TOOL"},

    "tab_veo3": {"vi": "VEO 3", "en": "VEO 3"},
    "tab_grok": {"vi": "GROK", "en": "GROK"},
    "tab_text_to_video": {"vi": "Text to Video", "en": "Text to Video"},
    "tab_image_to_video": {"vi": "Image to Video", "en": "Image to Video"},
    "tab_idea_to_video": {"vi": "Ý tưởng → Video", "en": "Idea → Video"},
    "tab_character_sync": {"vi": "Video Đồng Nhất", "en": "Character Sync"},
    "tab_create_image": {"vi": "Tạo Ảnh", "en": "Create Image"},
    "tab_settings": {"vi": "Cài đặt", "en": "Settings"},

    "btn_create_video": {"vi": "▶  Tạo video", "en": "▶  Generate"},
    "btn_stop": {"vi": "■  Dừng", "en": "■  Stop"},
    "btn_view_output": {"vi": "📂  Xem video/Ảnh", "en": "📂  View Output"},
    "btn_add_to_queue": {"vi": "Thêm vào hàng chờ", "en": "Add to Queue"},

    "lbl_aspect_ratio": {"vi": "Tỷ lệ khung hình", "en": "Aspect Ratio"},
    "lbl_veo_model": {"vi": "Model VEO", "en": "VEO Model"},
    "lbl_output_dir": {"vi": "Thư mục lưu video", "en": "Output Directory"},
    "aspect_portrait": {"vi": "Dọc 9:16", "en": "Portrait 9:16"},
    "aspect_landscape": {"vi": "Ngang 16:9", "en": "Landscape 16:9"},

    "prompt_title": {"vi": "Nhập prompt (mỗi dòng là 1 prompt)", "en": "Enter prompts (one per line)"},
    "prompt_placeholder": {
        "vi": "Nhập prompt ở đây. Mỗi prompt là 1 dòng.\nVí dụ:\n- Một con mèo đeo kính đang đọc sách trong quán cà phê\n- Cảnh hoàng hôn trên biển, phong cách cinematic",
        "en": "Enter prompts here. Each line is one prompt.\nExamples:\n- A cat wearing glasses reading a book in a cafe\n- Sunset over the ocean, cinematic style",
    },

    "settings_group_timing": {"vi": "Cài đặt thời gian", "en": "Timing Settings"},
    "settings_group_video": {"vi": "Cài đặt Video", "en": "Video Settings"},
    "settings_group_veo3_account": {"vi": "Tài khoản VEO3", "en": "VEO3 Account"},
    "settings_group_gemini": {"vi": "Gemini API Keys", "en": "Gemini API Keys"},

    "settings_multi_video": {"vi": "Số video chạy đồng thời", "en": "Concurrent Videos"},
    "settings_output_count": {"vi": "Số lượng output mỗi prompt", "en": "Outputs per Prompt"},
    "settings_wait_gen_video": {"vi": "Thời gian chờ tạo video (s)", "en": "Video Gen Wait (s)"},
    "settings_wait_gen_image": {"vi": "Thời gian chờ tạo ảnh (s)", "en": "Image Gen Wait (s)"},
    "settings_retry_error": {"vi": "Số lần retry khi lỗi", "en": "Retries on Error"},
    "settings_clear_data": {"vi": "Clear data sau N video", "en": "Clear Data after N Videos"},
    "settings_clear_data_wait": {"vi": "Thời gian chờ clear data (s)", "en": "Clear Data Wait (s)"},
    "settings_clear_data_image": {"vi": "Clear data image sau N ảnh", "en": "Clear Data after N Images"},
    "settings_wait_resend": {"vi": "Thời gian chờ gửi lại video (s)", "en": "Resend Wait (s)"},
    "settings_download_mode": {"vi": "Chế độ tải", "en": "Download Mode"},
    "settings_token_option": {"vi": "Token Option", "en": "Token Option"},
    "settings_seed_mode": {"vi": "Chế độ Seed", "en": "Seed Mode"},
    "settings_seed_value": {"vi": "Giá trị Seed", "en": "Seed Value"},
    "settings_email": {"vi": "Email", "en": "Email"},
    "settings_password": {"vi": "Mật khẩu", "en": "Password"},
    "settings_account_type": {"vi": "Loại tài khoản", "en": "Account Type"},

    "btn_save": {"vi": "💾  Lưu cài đặt", "en": "💾  Save Settings"},
    "btn_open_profile": {"vi": "Mở Profile Chrome", "en": "Open Chrome Profile"},
    "btn_save_token": {"vi": "Lưu Token", "en": "Save Token"},
    "btn_auto_login": {"vi": "Tự động đăng nhập", "en": "Auto Login"},
    "btn_delete_profile": {"vi": "Xóa Profile", "en": "Delete Profile"},
    "btn_close_chrome": {"vi": "Đóng Chrome", "en": "Close Chrome"},

    "msg_save_success": {"vi": "Đã lưu cài đặt thành công!", "en": "Settings saved successfully!"},
    "msg_no_prompt": {"vi": "Hãy nhập ít nhất một prompt.", "en": "Please enter at least one prompt."},
    "msg_confirm": {"vi": "Xác nhận", "en": "Confirm"},
    "msg_info": {"vi": "Thông báo", "en": "Information"},
    "msg_warning": {"vi": "Cảnh báo", "en": "Warning"},
    "msg_error": {"vi": "Lỗi", "en": "Error"},

    "img_btn_add": {"vi": "➕ Thêm ảnh", "en": "➕ Add Images"},
    "img_btn_clear": {"vi": "🗑 Xóa tất cả", "en": "🗑 Clear All"},
    "img_col_stt": {"vi": "STT", "en": "#"},
    "img_col_image": {"vi": "Ảnh", "en": "Image"},
    "img_col_prompt": {"vi": "Prompt", "en": "Prompt"},
    "img_col_start": {"vi": "Ảnh đầu", "en": "Start Image"},
    "img_col_end": {"vi": "Ảnh cuối", "en": "End Image"},

    "status_col_check": {"vi": "", "en": ""},
    "status_col_stt": {"vi": "STT", "en": "#"},
    "status_col_video": {"vi": "Video", "en": "Video"},
    "status_col_status": {"vi": "Trạng thái", "en": "Status"},
    "status_col_mode": {"vi": "Chế độ", "en": "Mode"},
    "status_col_prompt": {"vi": "Prompt", "en": "Prompt"},

    "status_waiting": {"vi": "Đang chờ", "en": "Waiting"},
    "status_running": {"vi": "Đang chạy", "en": "Running"},
    "status_done": {"vi": "Hoàn thành", "en": "Completed"},
    "status_error": {"vi": "Lỗi", "en": "Error"},

    "status_total": {"vi": "Tổng", "en": "Total"},
    "status_completed": {"vi": "Xong", "en": "Done"},
    "status_failed": {"vi": "Lỗi", "en": "Failed"},

    "idea_project_name": {"vi": "Tên dự án", "en": "Project Name"},
    "idea_text": {"vi": "Nội dung ý tưởng", "en": "Idea Content"},
    "idea_scene_count": {"vi": "Số cảnh", "en": "Scene Count"},
    "idea_style": {"vi": "Phong cách", "en": "Style"},
    "idea_language": {"vi": "Ngôn ngữ lời thoại", "en": "Dialogue Language"},

    "char_sync_ref_image": {"vi": "Ảnh tham chiếu nhân vật", "en": "Character Reference Image"},
    "char_sync_prompts": {"vi": "Nhập prompt cho các video", "en": "Enter prompts for videos"},

    "create_image_model": {"vi": "Model tạo ảnh", "en": "Image Model"},
    "create_image_prompt": {"vi": "Nhập prompt tạo ảnh", "en": "Enter image prompt"},

    "grok_settings_account": {"vi": "Tài khoản GROK", "en": "GROK Account"},
    "grok_video_length": {"vi": "Thời lượng video (s)", "en": "Video Length (s)"},
    "grok_video_resolution": {"vi": "Độ phân giải", "en": "Resolution"},
    "grok_multi_video": {"vi": "Số video đồng thời", "en": "Concurrent Videos"},
    "tab_grok_settings": {"vi": "Cài đặt GROK", "en": "GROK Settings"},

    "theme_light": {"vi": "☀️  Sáng", "en": "☀️  Light"},
    "theme_dark": {"vi": "🌙  Tối", "en": "🌙  Dark"},
    "lang_vi": {"vi": "🇻🇳 Tiếng Việt", "en": "🇻🇳 Vietnamese"},
    "lang_en": {"vi": "🇺🇸 English", "en": "🇺🇸 English"},

    "lbl_account_info": {"vi": "Thông tin tài khoản", "en": "Account Info"},
    "lbl_license_expiry": {"vi": "Hạn sử dụng", "en": "Expiry Date"},
    "lbl_plan": {"vi": "Gói", "en": "Plan"},

    "btn_zalo_group": {"vi": "💬 Nhóm Zalo", "en": "💬 Zalo Group"},
    "btn_guide": {"vi": "📖 Hướng dẫn", "en": "📖 User Guide"},

    "log_title": {"vi": "Nhật ký chạy", "en": "Run Log"},

    "btn_join_video": {"vi": "Nối video", "en": "Join Video"},
    "btn_remove_watermark": {"vi": "Xóa logo", "en": "Remove Logo"},
    "btn_retry": {"vi": "Tạo lại video", "en": "Regenerate"},
    "btn_retry_failed": {"vi": "Tạo lại video lỗi", "en": "Retry Failed"},
    "btn_cut_last": {"vi": "Cắt ảnh cuối", "en": "Trim Last Frame"},
    "btn_del": {"vi": "Xóa kết quả", "en": "Delete Results"},
    "lbl_account": {"vi": "Tài khoản:", "en": "Account:"},
    "lbl_account_type": {"vi": "Loại tài khoản:", "en": "Account Type:"},
    "lbl_expiry": {"vi": "Ngày hết hạn:", "en": "Expiry:"},

    "queue_confirm_title": {"vi": "Thêm vào hàng chờ?", "en": "Add to Queue?"},
    "queue_confirm_msg": {
        "vi": "Bạn muốn thêm {count} prompt vào hàng chờ {workflow}?",
        "en": "Add {count} prompts to {workflow} queue?",
    },
    "queue_success_msg": {
        "vi": "Đã thêm {count} prompt vào hàng chờ {workflow}.",
        "en": "Added {count} prompts to {workflow} queue.",
    },

    "tab_single_image": {"vi": "Tạo Video Từ Ảnh", "en": "Image to Video"},
    "tab_start_end_image": {"vi": "Ảnh Đầu - Cuối", "en": "Start & End Image"},
    "tab_create_from_prompt": {"vi": "Tạo Ảnh Từ Prompt", "en": "Create from Prompt"},
}


def t(key: str, **kwargs) -> str:
    entry = TRANSLATIONS.get(key)
    if not entry:
        return key
    text = entry.get(_current_lang, entry.get("vi", key))
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def current_lang() -> str:
    return _current_lang


def set_lang(lang: str) -> None:
    global _current_lang
    if lang not in ("vi", "en"):
        lang = "vi"
    _current_lang = lang
    _save_lang(lang)
    for fn in _listeners:
        try:
            fn()
        except Exception:
            pass


def on_lang_change(fn: Callable[[], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def remove_lang_listener(fn: Callable[[], None]) -> None:
    try:
        _listeners.remove(fn)
    except ValueError:
        pass


def _save_lang(lang: str) -> None:
    try:
        _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        _LANG_FILE.write_text(lang, encoding="utf-8")
    except Exception:
        pass


def _load_lang() -> str:
    try:
        if _LANG_FILE.exists():
            val = _LANG_FILE.read_text(encoding="utf-8").strip()
            if val in ("vi", "en"):
                return val
    except Exception:
        pass
    return "vi"


_current_lang = _load_lang()
