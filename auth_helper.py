import urllib.request
import urllib.error
import re
import json
import time
import threading

_LAST_TOKEN = ""
_LAST_TOKEN_TIME = 0
_REFRESH_LOCK = threading.Lock()
_TOKEN_TTL = 600  # 10 phút - refresh token trước khi hết hạn

def get_valid_access_token(cookie: str, project_id: str, force_refresh=False) -> str:
    """Lấy access_token hợp lệ.
    
    - Nếu token vẫn còn trong TTL (10 phút) và không force_refresh → trả về cached
    - Nếu hết hạn hoặc force_refresh → gọi API lấy token mới
    - Thread-safe với lock
    """
    global _LAST_TOKEN, _LAST_TOKEN_TIME
    now = time.time()
    
    from settings_manager import SettingsManager
    try:
        config = SettingsManager.load_config()
    except Exception:
        config = {}
        
    account = config.get("account1", {}) if isinstance(config, dict) else {}
    current_token = account.get("access_token", "")
    
    # Cache hit: token vẫn hợp lệ trong TTL
    if not force_refresh and current_token and current_token == _LAST_TOKEN and (now - _LAST_TOKEN_TIME < _TOKEN_TTL):
        return current_token

    if not cookie or not project_id:
        return current_token

    # Thread-safe refresh
    with _REFRESH_LOCK:
        # Double-check sau khi acquire lock (có thể thread khác đã refresh)
        now2 = time.time()
        if not force_refresh and _LAST_TOKEN and (now2 - _LAST_TOKEN_TIME < _TOKEN_TTL):
            return _LAST_TOKEN

        # Thử lấy token mới từ API
        new_token = _fetch_token_from_api(cookie, project_id)
        
        if new_token:
            _LAST_TOKEN = new_token
            _LAST_TOKEN_TIME = time.time()
            
            # Lưu vào config nếu token thay đổi
            if new_token != current_token:
                try:
                    # Reload config mới nhất trước khi save (tránh ghi đè)
                    config = SettingsManager.load_config()
                    if "account1" not in config:
                        config["account1"] = {}
                    config["account1"]["access_token"] = new_token
                    SettingsManager.save_config(config)
                except Exception as e:
                    print(f"⚠️ Lỗi lưu access_token vào config: {e}")
            return new_token
        
        # Nếu force_refresh mà vẫn không lấy được token mới,
        # không nên trả lại token cũ vì token cũ cũng đã bị 401 rồi.
        if force_refresh:
            return ""

    return current_token


def _fetch_token_from_api(cookie: str, project_id: str) -> str:
    """Gọi API để lấy access_token mới từ cookie."""
    for attempt in range(2):  # Retry 1 lần nếu thất bại
        try:
            url = f"https://labs.google/fx/vi/tools/flow/project/{project_id}?_nocache={int(time.time()*1000)}"
            req = urllib.request.Request(url, headers={
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                m = re.search(r'"access_token":"([^"]+)"', html)
                if m:
                    return m.group(1)
                else:
                    print(f"⚠️ auth_helper: Không tìm thấy access_token trong HTML (attempt {attempt+1})")
        except urllib.error.HTTPError as e:
            print(f"⚠️ auth_helper: HTTP {e.code} khi lấy token (attempt {attempt+1})")
            if e.code == 403:
                # Cookie có thể hết hạn, không retry
                break
        except Exception as e:
            print(f"⚠️ auth_helper: Lỗi lấy access_token (attempt {attempt+1}): {e}")
        
        if attempt < 1:
            time.sleep(2)
    
    return ""


def invalidate_cache():
    """Xóa cache token để buộc refresh lần gọi tiếp theo."""
    global _LAST_TOKEN, _LAST_TOKEN_TIME
    _LAST_TOKEN = ""
    _LAST_TOKEN_TIME = 0
