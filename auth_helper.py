import urllib.request
import re
import json
import time

_LAST_TOKEN = ""
_LAST_TOKEN_TIME = 0

def get_valid_access_token(cookie: str, project_id: str, force_refresh=False) -> str:
    global _LAST_TOKEN, _LAST_TOKEN_TIME
    now = time.time()
    
    from settings_manager import SettingsManager
    try:
        config = SettingsManager.load_config()
    except Exception:
        config = {}
        
    account = config.get("account1", {}) if isinstance(config, dict) else {}
    current_token = account.get("access_token", "")
    
    if not force_refresh and current_token == _LAST_TOKEN and (now - _LAST_TOKEN_TIME < 1800):
        return current_token

    if not cookie or not project_id:
        return current_token

    try:
        url = f"https://labs.google/fx/vi/tools/flow/project/{project_id}"
        req = urllib.request.Request(url, headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'"access_token":"([^"]+)"', html)
            if m:
                new_token = m.group(1)
                _LAST_TOKEN = new_token
                _LAST_TOKEN_TIME = now
                
                if new_token != current_token:
                    if "account1" not in config:
                        config["account1"] = {}
                    config["account1"]["access_token"] = new_token
                    SettingsManager.save_config(config)
                return new_token
    except Exception as e:
        print("Loi lay access_token:", e)
        pass
        
    return current_token
