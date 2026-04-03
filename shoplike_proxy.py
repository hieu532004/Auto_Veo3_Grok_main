import os
import random
import threading
from pathlib import Path
from typing import Optional, List
import requests

from settings_manager import DATA_GENERAL_DIR

# -------------------------------------------------------------------
# Paths
# -------------------------------------------------------------------
SHOPLIKE_KEYS_FILE = Path(DATA_GENERAL_DIR) / "shoplike_keys.txt"
PROXYVN_KEYS_FILE = Path(DATA_GENERAL_DIR) / "proxyvn_keys.txt"
PROXY_MODE_FILE = Path(DATA_GENERAL_DIR) / "proxy_mode.txt"

# -------------------------------------------------------------------
# Proxy mode constants
# -------------------------------------------------------------------
PROXY_MODE_NONE = "none"           # Không dùng proxy
PROXY_MODE_HTTP = "http"           # Proxy HTTP/SOCKS5 tĩnh (proxies.txt)
PROXY_MODE_SHOPLIKE = "shoplike"   # ShopLike rotating proxy
PROXY_MODE_PROXYVN = "proxyvn"     # Proxy.vn rotating proxy

PROXY_MODES = [PROXY_MODE_NONE, PROXY_MODE_HTTP, PROXY_MODE_SHOPLIKE, PROXY_MODE_PROXYVN]
PROXY_MODE_LABELS = {
    PROXY_MODE_NONE: "Không Fake IP",
    PROXY_MODE_HTTP: "Proxy theo List (Hỗ trợ Tĩnh & Xoay)",
    PROXY_MODE_SHOPLIKE: "Proxy ShopLike (Xoay IP)",
    PROXY_MODE_PROXYVN: "Proxy Proxy.vn (Xoay IP)",
}

def get_current_proxy_mode() -> str:
    try:
        if PROXY_MODE_FILE.exists():
            mode = PROXY_MODE_FILE.read_text(encoding="utf-8").strip()
            if mode in PROXY_MODES:
                return mode
    except Exception:
        pass
    return PROXY_MODE_NONE

def set_proxy_mode(mode: str) -> None:
    if mode not in PROXY_MODES:
        mode = PROXY_MODE_NONE
    try:
        PROXY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROXY_MODE_FILE.write_text(mode, encoding="utf-8")
    except Exception:
        pass

def load_shoplike_keys() -> List[str]:
    try:
        if SHOPLIKE_KEYS_FILE.exists():
            lines = SHOPLIKE_KEYS_FILE.read_text(encoding="utf-8").splitlines()
            return [ln.strip() for ln in lines if ln.strip()]
    except Exception:
        pass
    return []

def save_shoplike_keys(keys: List[str]) -> None:
    try:
        SHOPLIKE_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHOPLIKE_KEYS_FILE.write_text("\n".join(keys), encoding="utf-8")
    except Exception:
        pass

def load_proxyvn_keys() -> List[str]:
    try:
        if PROXYVN_KEYS_FILE.exists():
            lines = PROXYVN_KEYS_FILE.read_text(encoding="utf-8").splitlines()
            return [ln.strip() for ln in lines if ln.strip()]
    except Exception:
        pass
    return []

def save_proxyvn_keys(keys: List[str]) -> None:
    try:
        PROXYVN_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROXYVN_KEYS_FILE.write_text("\n".join(keys), encoding="utf-8")
    except Exception:
        pass

class ShopLikeProxy:
    BASE_URL = "http://proxy.shoplike.vn/Api"
    _lock = threading.Lock()
    _last_proxy = None

    def __init__(self, api_keys=None):
        self.api_keys = [k.strip() for k in (api_keys or []) if k.strip()]
        if not self.api_keys:
            self.api_keys = load_shoplike_keys()

    def get_new_proxy(self, key: str, location: str = "", provider: str = "") -> Optional[str]:
        try:
            url = f"{self.BASE_URL}/getNewProxy?access_token={key}"
            if location: url += f"&location={location}"
            if provider: url += f"&provider={provider}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("status") == "success":
                proxy = data["data"]["proxy"]
                with self._lock:
                    ShopLikeProxy._last_proxy = proxy
                return proxy
            if data.get("status") == "error" and "nextChange" in resp.text:
                return self.get_current_proxy(key)
        except Exception as e:
            print(f"[ShopLike] Lỗi getNewProxy: {e}")
        return None

    def get_current_proxy(self, key: str) -> Optional[str]:
        try:
            resp = requests.get(f"{self.BASE_URL}/getCurrentProxy?access_token={key}", timeout=10)
            data = resp.json()
            if data.get("status") == "success":
                proxy = data["data"]["proxy"]
                with self._lock:
                    ShopLikeProxy._last_proxy = proxy
                return proxy
        except Exception:
            pass
        return None

    def get_random_proxy(self, max_retries: int = 20) -> Optional[str]:
        for _ in range(max_retries):
            if not self.api_keys: return None
            key = random.choice(self.api_keys)
            proxy = self.get_new_proxy(key)
            if proxy: return proxy
        return None

class ProxyVNClient:
    _lock = threading.Lock()
    _last_proxy = None
    _cached_keyxoay = []
    _last_fetch_time = 0

    def __init__(self, api_keys=None):
        self.api_keys = [k.strip() for k in (api_keys or []) if k.strip()]
        if not self.api_keys:
            self.api_keys = load_proxyvn_keys()

    def get_all_keyxoay(self, key: str) -> List[str]:
        try:
            import re
            url = f"https://proxy.vn/proxyxoay/apigetkeyxoay.php?key={key}"
            resp = requests.get(url, timeout=10)
            matches = re.findall(r'"keyxoay"\s*:\s*"([^"]+)"', resp.text)
            if matches: return list(set(matches))
        except Exception:
            pass
        return []

    def get_proxy(self, keyxoay: str) -> Optional[str]:
        try:
            url = f"https://proxyxoay.shop/api/get.php?key={keyxoay}&nhamang=random&tinhthanh=0&whitelist="
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if int(data.get("status", 0)) == 100:
                proxyhttp = data.get("proxyhttp", "")
                if proxyhttp:
                    parts = [p for p in proxyhttp.split(':') if p.strip()]
                    proxy = ':'.join(parts)
                    with self._lock:
                        ProxyVNClient._last_proxy = proxy
                    return proxy
            elif int(data.get("status", 0)) in [101, 102]:
                return self.get_last_proxy()
        except Exception:
            pass
        return self.get_last_proxy()

    def get_random_proxy(self, max_retries: int = 10) -> Optional[str]:
        import time
        with self._lock:
            now = time.time()
            if not self._cached_keyxoay or (now - getattr(self, '_last_fetch_time', 0) > 300):
                self._cached_keyxoay = []
                for k in self.api_keys:
                    k_list = self.get_all_keyxoay(k)
                    if k_list: self._cached_keyxoay.extend(k_list)
                    else: self._cached_keyxoay.append(k)
                self._last_fetch_time = now

        for _ in range(max_retries):
            if not self._cached_keyxoay: return None
            kx = random.choice(self._cached_keyxoay)
            px = self.get_proxy(kx)
            if px: return px
        return None

    @classmethod
    def get_last_proxy(cls) -> Optional[str]:
        with cls._lock: return cls._last_proxy

def resolve_proxy_for_chrome() -> Optional[str]:
    mode = get_current_proxy_mode()
    if mode == PROXY_MODE_NONE: return None
    if mode == PROXY_MODE_HTTP:
        try:
            pfile = Path(DATA_GENERAL_DIR) / "proxies.txt"
            if pfile.exists():
                with open(pfile, "r", encoding="utf-8") as f:
                    plist = [ln.strip() for ln in f if ln.strip()]
                if plist: return random.choice(plist)
        except Exception: pass
        return None
    if mode == PROXY_MODE_SHOPLIKE:
        return ShopLikeProxy().get_random_proxy()
    if mode == PROXY_MODE_PROXYVN:
        return ProxyVNClient().get_random_proxy()
    return None
