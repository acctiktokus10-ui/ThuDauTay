"""
bot_data_loader.py
──────────────────────────────────────────────────────────────────
Fetch donhang và vitien từ Vercel thay vì đọc file local.
da_nhan_by_subid.json vẫn đọc/ghi local như cũ — không đổi.
──────────────────────────────────────────────────────────────────
"""

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

# ── CẤU HÌNH ────────────────────────────────────────────────────
VERCEL_BASE_URL = "https://thudautay.vercel.app"
# ────────────────────────────────────────────────────────────────

_CACHE: dict = {}


def _fetch_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ZaloBot/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_remote(type_: str, fallback_path: str) -> dict:
    url = f"{VERCEL_BASE_URL}/api/data/{type_}"
    try:
        data = _fetch_json(url)
        _CACHE[type_] = data
        log.info(f"[Remote] Đã tải {len(data)} sub_id ({type_}) từ Vercel")
        return data
    except Exception as e:
        log.warning(f"⚠️ Không lấy được {type_} từ Vercel ({e}), thử đọc file local...")
        try:
            with open(fallback_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info(f"[Local fallback] Đọc {len(data)} sub_id từ {fallback_path}")
            return data
        except FileNotFoundError:
            log.warning(f"⚠️ Không tìm thấy {fallback_path} — dùng cache cũ hoặc rỗng")
            return _CACHE.get(type_, {})


def load_donhang_remote(fallback_path: str = "donhang_by_subid.json") -> dict:
    return _load_remote("donhang", fallback_path)


def load_vitien_remote(fallback_path: str = "vitien_by_subid.json") -> dict:
    return _load_remote("vitien", fallback_path)
