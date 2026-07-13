"""
bot_data_loader.py
──────────────────────────────────────────────────────────────────
Fetch donhang và vitien từ Vercel (thudautay) thay vì đọc file local.
da_nhan_by_subid.json vẫn đọc/ghi local như cũ — không đổi.

[MỚI] push_danhan_remote(): sau khi bot ghi da_nhan cục bộ (ví dụ sau
lệnh #ruttien), gọi hàm này để đẩy dữ liệu da_nhan mới nhất lên CẢ HAI web:
  1. thudautay      (Vercel + Upstash Redis) — qua POST /api/data/danhan
  2. hoantien-dautay (Vercel + Upstash Redis, dùng CHUNG database với
                       thudautay) — qua POST /api/sync-data
Hai web giờ đọc/ghi chung 1 Upstash Redis nên 2 lần gửi bên dưới thực chất
ghi đè cùng 1 key `danhan_by_subid` — không sai, chỉ hơi dư, không cần xoá
vì vẫn hoạt động đúng và giữ nguyên cơ chế xác thực riêng của từng web
(BOT_SECRET / SYNC_SECRET). Có thể bỏ bớt 1 trong 2 lần gửi nếu muốn gọn hơn.
Hai lần gửi độc lập nhau: một bên lỗi không ảnh hưởng bên còn lại.
──────────────────────────────────────────────────────────────────
"""

import json
import logging
import urllib.request
import urllib.error
import urllib.parse

log = logging.getLogger(__name__)

# ── CẤU HÌNH ────────────────────────────────────────────────────
# Web quản lý dữ liệu chính (thudautay) — bot đọc donhang/vitien tại đây
# ⚠️ Giả định domain mặc định Vercel theo tên project "thudautay". Nếu
# project của bạn có custom domain khác, sửa lại dòng dưới cho đúng domain
# thật (Vercel → project thudautay → Settings → Domains).
VERCEL_BASE_URL = "https://thudautay.vercel.app"

# Web hiển thị cho khách (hoantien-dautay) — nhận đồng bộ donhang/vitien/da_nhan
HOANVI_BASE_URL = "https://hoantien-dautay.vercel.app"

# Secret dùng để xác thực khi bot/thudautay ghi dữ liệu lên (phải khớp
# với biến môi trường BOT_SECRET trên thudautay và SYNC_SECRET trên hoantien-dautay).
BOT_SECRET = ""    # để trống nếu thudautay không đặt BOT_SECRET
SYNC_SECRET = "meozzcutemeozzcutemeozzcutemeozzcutemeozzcutemeozzcutemeozzcutemeozzcutemeozzcutemeozzcute9999"

# ── Upstash Redis REST API ─────────────────────────────────────
# [MỚI] Lấy từ Upstash Console > database mới của hệ thống thudautay /
# hoantien-dautay > Connect > REST.
# QUAN TRỌNG: đây là database Redis DÙNG CHUNG giữa thudautay và
# hoantien-dautay (xem ghi chú đầu file) — nên bot chỉ cần ghi 1 LẦN DUY NHẤT
# vào đây là cả 2 web đều tự đọc thấy dữ liệu mới, không cần gọi thêm
# /api/sync-data hay /api/data/danhan qua HTTP của từng web nữa.
# ⚠️ Token này có toàn quyền đọc/ghi Redis — giữ kín file này, không commit
# lên Git/public repo. Khuyến khích chuyển sang biến môi trường khi triển khai
# thật (os.environ.get("UPSTASH_REDIS_REST_URL") ...).
UPSTASH_REDIS_REST_URL = "https://adapting-tomcat-79705.upstash.io"
UPSTASH_REDIS_REST_TOKEN = "gQAAAAAAATdZAAIgcDIyNDY2MTY2OThkYjE0M2M1OTY1OTc5MmY4ZTFlMzY0OQ"
# ────────────────────────────────────────────────────────────────

_CACHE: dict = {}


def _fetch_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ZaloBot/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 10) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": "ZaloBot/1.0",
            "Content-Type": "application/json",
            **headers,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


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


def push_danhan_remote(data: dict) -> dict:
    """Đẩy da_nhan_by_subid mới nhất lên thudautay VÀ hoantien-dautay.

    Trả về {"thudautay": bool, "hoanvi": bool} để bot log/biết bên nào lỗi.
    Không raise — lỗi mạng ở 1 bên không làm hỏng bên kia hay crash bot.
    """
    result = {"thudautay": False, "hoanvi": False}

    # 1) thudautay — POST /api/data/danhan  (đã có sẵn trong web)
    try:
        headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
        result["thudautay"] = _post_json(
            f"{VERCEL_BASE_URL}/api/data/danhan",
            {"data": data},
            headers,
        )
        log.info("📤 Đã đẩy da_nhan lên thudautay")
    except Exception as e:
        log.warning(f"⚠️ Đẩy da_nhan lên thudautay thất bại: {e}")

    # 2) hoantien-dautay — POST /api/sync-data (giờ ghi vào cùng Upstash Redis
    #    với thudautay, xem ghi chú ở đầu file)
    try:
        headers = {"X-Sync-Secret": SYNC_SECRET} if SYNC_SECRET else {}
        result["hoanvi"] = _post_json(
            f"{HOANVI_BASE_URL}/api/sync-data",
            {"type": "danhan", "data": data},
            headers,
        )
        log.info("📤 Đã đẩy da_nhan lên hoantien-dautay")
    except Exception as e:
        log.warning(f"⚠️ Đẩy da_nhan lên hoantien-dautay thất bại: {e}")

    return result


def push_danhan_from_file(file_path: str = "da_nhan_by_subid.json") -> dict:
    """[MỚI] Đọc TOÀN BỘ nội dung file da_nhan_by_subid.json từ đĩa,
    rồi gọi push_danhan_remote() để đẩy đúng dữ liệu đã ghi lên web.

    Dùng hàm này thay vì truyền thẳng dict trong RAM cho push_danhan_remote(),
    để đảm bảo dữ liệu gửi đi luôn khớp 100% với những gì đã thực sự nằm
    trên đĩa (file da_nhan_by_subid.json), tránh lệch dữ liệu nếu có tiến
    trình khác cũng ghi vào file này.

    Trả về {"thudautay": bool, "hoanvi": bool}, giống push_danhan_remote().
    Nếu đọc file lỗi (không tồn tại / hỏng định dạng), trả về cả hai False
    và không gửi gì lên web.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning(f"⚠️ Không tìm thấy {file_path} — bỏ qua đồng bộ da_nhan.")
        return {"thudautay": False, "hoanvi": False}
    except Exception as e:
        log.error(f"❌ Lỗi đọc {file_path} để đồng bộ da_nhan: {e}")
        return {"thudautay": False, "hoanvi": False}

    return push_danhan_remote(data)


def _upstash_kv_set(key: str, value, max_retries: int = 3) -> bool:
    """Ghi 1 key vào Upstash Redis REST API — viết giống HỆT hàm kvSet()
    trong lib/botData.js (hoantien-dautay) và pages/api/data/[type].js
    (thudautay): giá trị được json.dumps 1 lần thành chuỗi, rồi chuỗi
    đó được json.dumps lần nữa để làm body gửi đi (double-encode), để khi
    web đọc lại bằng kvGet() (parse JSON 1 lần) ra đúng chuỗi JSON gốc,
    và code hiện tại của web tự parse tiếp lần 2 nếu cần.

    [MỚI] Tự động thử lại tối đa max_retries lần nếu gặp lỗi kết nối tạm
    thời (ví dụ WinError 10054 — remote host đóng kết nối đột ngột), để
    tránh báo lỗi oan khi chỉ là trục trặc mạng thoáng qua.
    """
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        log.error("❌ Thiếu UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN — không thể ghi Upstash.")
        return False
    value_str = json.dumps(value, ensure_ascii=False)
    body = json.dumps(value_str).encode("utf-8")
    url = f"{UPSTASH_REDIS_REST_URL}/set/{urllib.parse.quote(key, safe='')}"

    last_err = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
                "Content-Type": "application/json",
                "Connection": "close",  # tránh tái sử dụng kết nối cũ có thể đã bị phía server đóng
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
                body_text = resp.read().decode("utf-8", errors="replace")
                log.error(f"❌ Ghi Upstash key='{key}' — HTTP {resp.status}: {body_text}")
                return False
        except urllib.error.HTTPError as e:
            # Upstash trả lỗi rõ ràng (vd 401 token sai) — không cần thử lại, sai là sai
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            log.error(f"❌ Ghi Upstash key='{key}' thất bại: HTTP {e.code} {err_body}")
            return False
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                log.warning(f"⚠️ Ghi Upstash key='{key}' lỗi tạm thời (lần {attempt}/{max_retries}): {e} — thử lại...")
                import time as _time
                _time.sleep(1.5 * attempt)  # chờ tăng dần: 1.5s, 3s, ...
            else:
                log.error(f"❌ Ghi Upstash key='{key}' thất bại sau {max_retries} lần thử: {e}")

    return False


def push_danhan_to_upstash(data: dict) -> bool:
    """[MỚI] Ghi thẳng da_nhan_by_subid lên Upstash Redis (REST API).
    Vì thudautay và hoantien-dautay dùng CHUNG 1 database Upstash, chỉ cần
    ghi 1 lần duy nhất ở đây là cả 2 web đều tự đọc thấy dữ liệu mới ngay,
    không cần gọi thêm HTTP request nào sang từng web nữa.

    Trả về True/False. Không raise.
    """
    from datetime import datetime as _dt
    ok = _upstash_kv_set("danhan_by_subid", data)
    if ok:
        _upstash_kv_set("meta_danhan", {
            "updated_at": _dt.utcnow().isoformat(),
            "count": len(data),
        })
        log.info(f"📤 Đã ghi thẳng danhan_by_subid lên Upstash ({len(data)} sub_id)")
    else:
        log.error("❌ Ghi danhan_by_subid lên Upstash thất bại")
    return ok


def push_danhan_from_file_to_upstash(file_path: str = "da_nhan_by_subid.json") -> bool:
    """Đọc TOÀN BỘ nội dung file da_nhan_by_subid.json từ đĩa, rồi ghi
    thẳng lên Upstash Redis (thay vì gọi HTTP sang web như push_danhan_from_file).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning(f"⚠️ Không tìm thấy {file_path} — bỏ qua đồng bộ Upstash.")
        return False
    except Exception as e:
        log.error(f"❌ Lỗi đọc {file_path} để đồng bộ Upstash: {e}")
        return False

    return push_danhan_to_upstash(data)
