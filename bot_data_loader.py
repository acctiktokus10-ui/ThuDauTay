"""
Zalo Shopee Affiliate Bot — v27
─────────────────────────────────────────────────────────────────────────────
Dựa trên bot_v23.py, nâng cấp theo bot_v23.py:

[v27] NÂNG CẤP TỪ v26:
  - _shorten_name: giới hạn tên sản phẩm tối đa 25 ký tự (hoa=1.3, thường=1.0)
  - _build_message / _build_message_parts: tên sản phẩm dùng limit 25, hoa=1.3
  - _handle_multi: xử lý TẤT CẢ link, chia 5 link / tin nhắn
      · 10 link → 2 tin, 13 link → 3 tin, 3 link → 1 tin
  - _listen_loop: nhóm link theo node_id trước khi gọi _handle / _handle_multi
  - getMsgIdFromNode: tìm data-qid cả trong con (querySelectorAll) trước khi leo lên cha
    → giải quyết trường hợp Zalo render node link là wrapper không có data-qid
  - _reply_to_message_by_link: hàm fallback mới — tìm bubble chứa link trong DOM
    → hover → click nút Trả lời, không cần msg_id
  - _handle: khi nhận link Shopee, ưu tiên reply by msg_id → fallback reply by link bubble
    → đảm bảo tin trả lời LUÔN được quote vào tin nhắn gốc của thành viên

[v25] NÂNG CẤP TỪ v1_converter:
  - JS Observer: thêm watchdog interval 2s tự re-attach khi container DOM thay đổi
  - _JS_DRAIN_QUEUE: trả về {items, joins} để xử lý sự kiện join nhóm (dự phòng)
  - _JS_CHECK_OBSERVER: thêm check hasWatchdog + containerClass chi tiết hơn
  - _JS_PURGE_LINK: logic filter theo queue thay vì xóa seenIds
  - _open_zalo: thêm các flags Chrome cho VPS/RDP (GPU swiftshader, mute-audio...)
  - _get_current_group_id: thêm DOM selector fallback và active conv-item
  - _mark_existing_links: thêm selector Message/Bubble hoa văn đầy đủ
  - _listen_loop: log Observer status rõ hơn (containerClass, queueLen)
  - _handle_command (#vitien): dùng _calc_vitien + _get_rank_vitien (chuẩn hơn)
  - _handle_command (#ruttien): ghi da_nhan theo format tháng {t5: ...} thay vì float đơn
  - _send_text_reply: helper dùng chung cho mọi lệnh (giữ từ v1, không có trong v23)
  - _build_message: giữ nguyên mẫu v1_converter
  - KHÔNG thay đổi: shopee_converter thay ExtensionBridge (vẫn dùng nội bộ)

Tính năng (giữ nguyên từ v1_converter):
  ✅ Nhận link s.shopee.vn / vn.shp.ee → convert affiliate qua shopee_converter
  ✅ Check hoa hồng song song (HTTP API)
  ✅ Tag @mention tên người gửi
  ✅ Lệnh #donhang / #donhangN
  ✅ Lệnh #vitien (+ BXH tổng / tháng)
  ✅ Lệnh #ruttien / #ruttien_<số>
  ✅ Lệnh #thongtin / #thongtin_<bank><số>
  ✅ Lệnh #topbxh / #topbxh_tN (chỉ Chutich)

Yêu cầu:
  pip install playwright aiohttp
  playwright install chromium
  Đặt shopee_converter.py cùng thư mục với bot này.

─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import logging
import re
import unicodedata as _ud
import re as _re
import concurrent.futures
from bot_wrap_patch import wrap_affiliate_link  # [v28] bọc affiliate → short link

from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse
from playwright.async_api import async_playwright

# ==================== SHOPEE CONVERTER (tích hợp sẵn) ====================

import urllib.parse as _urlparse
import urllib.request as _urlrequest

def _try_strptime(s: str, fmt: str):
    """Trả về True nếu parse được, dùng cho generator expression trong next()."""
    try:
        from datetime import datetime as _dtp
        _dtp.strptime(s, fmt)
        return True
    except (ValueError, TypeError):
        return False

DEFAULT_AFFILIATE_ID = "17332000392"
DEFAULT_SUB_ID       = ""   # Không dùng sub_id cố định — lấy từ Zalo ID người gửi

def _sc_extract_shop_product(url: str):
    """Trích (shop_id, product_id) từ URL chứa /product/ hoặc /opaanlp/"""
    m = re.search(r"shopee\.vn/(?:product|opaanlp)/(\d+)/(\d+)", url)
    return (m.group(1), m.group(2)) if m else None

def _sc_resolve_short_link(url: str) -> str:
    """Follow HTTP redirect, trả URL đích — timeout giảm còn 5s"""
    req = _urlrequest.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _urlrequest.urlopen(req, timeout=5) as resp:
        return resp.url

# _sc_resolve_short_link_async định nghĩa SAU phần COMMISSION CHECK (cần aiohttp session)

def _sc_parse_link(raw: str):
    raw = raw.strip()
    ids = _sc_extract_shop_product(raw)
    if ids:
        return ids
    m = re.search(r"origin_link=([^&]+)", raw)
    if m:
        ids = _sc_extract_shop_product(_urlparse.unquote(m.group(1)))
        if ids:
            return ids
    m = re.search(r"[?&]next=([^&]+)", raw)
    if m:
        ids = _sc_extract_shop_product(_urlparse.unquote(m.group(1)))
        if ids:
            return ids
    if re.search(r"(shp\.ee|shope\.ee|s\.shopee\.vn)/\w+", raw) or "vn.shp.ee" in raw:
        try:
            resolved = _sc_resolve_short_link(raw)
            ids = _sc_extract_shop_product(resolved)
            if ids:
                return ids
        except Exception as e:
            logging.getLogger(__name__).warning(f"shopee_converter fetch lỗi: {e}")
    return None

def shopee_convert(raw: str,
                   affiliate_id: str = DEFAULT_AFFILIATE_ID,
                   sub_id: str = DEFAULT_SUB_ID) -> dict:
    """Chuyển đổi 1 link Shopee sang affiliate link. Trả dict {success, affiliate_link, ...}"""
    ids = _sc_parse_link(raw)
    if not ids:
        return {"success": False, "error": "Không tìm thấy Shop ID / Product ID"}
    shop_id, product_id = ids
    origin   = f"https://shopee.vn/product/{shop_id}/{product_id}"
    encoded  = _urlparse.quote(origin, safe="")
    affiliate = (
        f"https://s.shopee.vn/an_redir"
        f"?origin_link={encoded}"
        f"&affiliate_id={affiliate_id}"
        f"&sub_id={sub_id}"
    )
    return {
        "success":        True,
        "shop_id":        shop_id,
        "product_id":     product_id,
        "origin_link":    origin,
        "affiliate_link": affiliate,
    }

# Thread pool để chạy shopee_converter (sync) không block event loop
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# Cache resolve short link để không fetch lại link giống nhau
_resolve_cache: dict = {}

# ==================== DỮ LIỆU ĐƠN HÀNG (#donhang) ====================

def _load_donhang(json_path: str = "donhang_by_subid.json") -> dict:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            f"⚠️ Không tìm thấy {json_path} — lệnh #donhang sẽ không hoạt động."
        )
        return {}

def _chuan_hoa_ten(ten: str) -> str:
    return ten.strip().replace(" ", "")

def _shorten_name(name: str, limit: float = 25.0) -> str:
    import re as _re2
    name = _re2.sub(r'\[.*?\]|\(.*?\)', '', name).strip()
    name = _re2.sub(r'\s+', ' ', name)
    cost, result = 0.0, []
    for ch in name:
        w = 1.3 if ch.isupper() else 1.0
        if cost + w > limit:
            result.append('...')
            break
        result.append(ch)
        cost += w
    return ''.join(result)

def _fmt_money(val) -> str:
    try:
        v = float(val)
        return f"{int(v):,}đ".replace(',', '.') if v == int(v) else f"{v:,.0f}đ".replace(',', '.')
    except Exception:
        return "0đ"

def _format_donhang(data: dict, sub_id: str) -> list:
    if sub_id not in data:
        return ["❌Rất tiếc! Không tìm thấy đơn hàng của bạn😿\n\n✅ Hãy quay lại kiểm tra vào\n👉SÁNG NGÀY MAI👈 khi ad Thư thông báo trên nhóm nếu bạn đặt trước 👉23h59p hôm nay👈 nhé!"]
    don_list = sorted(
        data[sub_id]["don_hang"],
        key=lambda d: d.get("ngay_dat_hang", ""),
        reverse=True,
    )
    CHUNK = 10
    total_pages = max(1, (len(don_list) + CHUNK - 1) // CHUNK)
    messages = []
    for page_idx, start in enumerate(range(0, len(don_list), CHUNK), 1):
        chunk = don_list[start:start + CHUNK]
        lines = [f"📩Trang {page_idx}/{total_pages}\n🛒ĐƠN HÀNG CỦA SẾP\n"]
        for i, don in enumerate(chunk, start + 1):
            name = don.get("ten_san_pham_rut_gon") or _shorten_name(don.get("ten_san_pham", ""))
            comm = float(don.get("hoa_hong_rong", 0) or 0)
            tick = "✅" if comm > 0 else "❌"
            trang_thai = don['trang_thai']
            ngay_hoan_thanh = don.get("ngay_hoan_thanh", "")
            if ngay_hoan_thanh and trang_thai == "Hoàn thành":
                try:
                    from datetime import datetime as _dt
                    for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                        try:
                            ngay_fmt = _dt.strptime(ngay_hoan_thanh, _fmt).strftime("%d/%m")
                            trang_thai = f"Hoàn thành({ngay_fmt})"
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass
            lines.append(
                f"{i:02d}.🛍️{name}\n"
                f"    🏷️ ID: {don['id_don_hang']}\n"
                f"    💰 Hoa hồng: {_fmt_money(comm)} {tick}\n"
                f"    📊 Trạng thái: {trang_thai}\n"
                f"────────────────"
            )
        if page_idx < total_pages:
            next_cmd = f"#donhang{page_idx + 1}"
            lines.append(f"📩Hãy nhắn {next_cmd} để xem tiếp các đơn hàng nhé SẾP!")
        else:
            lines.append("📩SẾP đã xem hết tất cả các đơn hàng! Hãy tiếp tục mua sắm và tiết kiệm theo cách thông minh nhé!")
        messages.append("\n".join(lines))
    return messages

def _format_donhang_page(data: dict, sub_id: str, page: int) -> str:
    if sub_id not in data:
        return "❌Rất tiếc! Không tìm thấy đơn hàng của bạn😿\n\n✅ Hãy quay lại kiểm tra vào\n👉SÁNG NGÀY MAI👈 khi ad Thư thông báo trên nhóm nếu bạn đặt trước 👉23h59p hôm nay👈 nhé!"
    pages = _format_donhang(data, sub_id)
    total_pages = len(pages)
    if page < 1 or page > total_pages:
        return f"🥀Oh no! Hiện tại SẾP chỉ có {total_pages}/{total_pages} trang đơn hàng haha!"
    return pages[page - 1]

_DONHANG_DATA: dict = {}

# ==================== DỮ LIỆU VÍ TIỀN (#vitien) ====================

def _load_vitien(json_path: str = "vitien_by_subid.json") -> dict:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            f"⚠️ Không tìm thấy {json_path} — lệnh #vitien sẽ không hoạt động."
        )
        return {}

def _calc_vitien(vitien_data: dict, da_nhan_data: dict, sub_id: str):
    """
    Tính toán số dư ví theo logic:
      co_the_rut = (hoa_hong - 10%) * 80% cho đơn hoàn thành >= 3 ngày
      co_the_rut_hien = co_the_rut - da_nhan
    Trả về dict: dang_cho, hoan_thanh_chua_rut, co_the_rut, da_nhan, co_the_rut_hien
    """
    from datetime import date, datetime
    if sub_id not in vitien_data:
        return None
    v = vitien_data[sub_id]
    today = date.today()

    dang_cho = round(float(v.get("dang_cho", 0) or 0) * 0.90 * 0.8, 2)

    hoan_thanh_tong = 0.0
    co_the_rut = 0.0
    for don in v.get("don_hoan_thanh", []):
        hh = float(don.get("hoa_hong_rong") or don.get("hoa_hong") or 0)
        ngay_str = don.get("ngay_hoan_thanh")
        so_ngay = 0
        if ngay_str:
            try:
                ngay_ht = next((datetime.strptime(ngay_str, fmt).date() for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d") if _try_strptime(ngay_str, fmt)), None)
                so_ngay = (today - ngay_ht).days
            except Exception:
                pass
        hoan_thanh_tong = round(hoan_thanh_tong + hh, 2)
        if so_ngay >= 1:
            co_the_rut = round(co_the_rut + round((hh * 0.90) * 0.8, 2), 2)

    da_nhan = _get_da_nhan(da_nhan_data, sub_id, thang=0)
    co_the_rut_hien = max(0.0, round(co_the_rut - da_nhan, 2))

    # Tính hoan_thanh_chua_rut = đơn hoàn thành < 3 ngày (chưa thể rút)
    hoan_thanh_chua_rut = 0.0
    for don in v.get("don_hoan_thanh", []):
        hh = float(don.get("hoa_hong_rong") or don.get("hoa_hong") or 0)
        ngay_str = don.get("ngay_hoan_thanh")
        so_ngay = 0
        if ngay_str:
            try:
                from datetime import datetime as _dtt
                ngay_ht = next((_dtt.strptime(ngay_str, fmt).date() for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d") if _try_strptime(ngay_str, fmt)), None)
                so_ngay = (today - ngay_ht).days
            except Exception:
                pass
        if so_ngay < 1:
            hoan_thanh_chua_rut = round(hoan_thanh_chua_rut + round((hh * 0.90) * 0.8, 2), 2)

    # Tổng số tiền (chưa trừ thuế) của các đơn đã hoàn thành TRƯỚC ngày 06/07
    moc_0607 = date(today.year, 7, 6)
    tong_truoc_0607 = 0.0
    for don in v.get("don_hoan_thanh", []):
        hh = float(don.get("hoa_hong_rong") or don.get("hoa_hong") or 0)
        ngay_str = don.get("ngay_hoan_thanh")
        if ngay_str:
            try:
                ngay_ht = next((datetime.strptime(ngay_str, fmt).date() for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d") if _try_strptime(ngay_str, fmt)), None)
                if ngay_ht and ngay_ht < moc_0607:
                    tong_truoc_0607 = round(tong_truoc_0607 + hh, 2)
            except Exception:
                pass

    return {
        "dang_cho": dang_cho,
        "hoan_thanh_chua_rut": hoan_thanh_chua_rut,
        "co_the_rut": co_the_rut,
        "co_the_rut_hien": co_the_rut_hien,
        "da_nhan": da_nhan,
        "tong_truoc_0607": tong_truoc_0607,
    }

def _get_rank_vitien(vitien_data: dict, da_nhan_data: dict, sub_id: str):
    """
    Tính vị trí BXH tổng và BXH tháng hiện tại.
    Trả về (rank_tong, rank_thang, thang_hien_tai).
    """
    from datetime import date, datetime, timedelta
    today = date.today()
    thang_hien_tai = today.month

    scores_tong = {}
    scores_thang = {}

    for sid, v in vitien_data.items():
        co_the_rut = 0.0
        tong_thang = 0.0
        for don in v.get("don_hoan_thanh", []):
            hh = float(don.get("hoa_hong_rong") or don.get("hoa_hong") or 0)
            ngay_str = don.get("ngay_hoan_thanh")
            so_ngay = 0
            ngay_ht = None
            if ngay_str:
                try:
                    ngay_ht = next((datetime.strptime(ngay_str, fmt).date() for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d") if _try_strptime(ngay_str, fmt)), None)
                    so_ngay = (today - ngay_ht).days
                except Exception:
                    pass
            if so_ngay >= 1:
                co_the_rut = round(co_the_rut + round((hh * 0.90) * 0.8, 2), 2)
                if ngay_ht:
                    ngay_co_the_rut = ngay_ht + timedelta(days=3)
                    if ngay_co_the_rut.month == thang_hien_tai and ngay_co_the_rut.year == today.year:
                        tong_thang = round(tong_thang + round((hh * 0.90) * 0.8, 2), 2)

        da_nhan = _get_da_nhan(da_nhan_data, sid, thang=0)
        tong = max(0.0, round(co_the_rut - da_nhan, 2)) + da_nhan
        scores_tong[sid] = round(tong, 2)
        scores_thang[sid] = round(tong_thang, 2)

    sorted_tong = sorted(scores_tong.items(), key=lambda x: x[1], reverse=True)
    sorted_thang = sorted(scores_thang.items(), key=lambda x: x[1], reverse=True)

    rank_tong = next((i + 1 for i, (s, _) in enumerate(sorted_tong) if s == sub_id), None)
    rank_thang = next((i + 1 for i, (s, _) in enumerate(sorted_thang) if s == sub_id), None)
    return rank_tong, rank_thang, thang_hien_tai

def _rank_label(rank: int) -> str:
    labels = [
        "Top 1👑 Tỷ Phú",
        "Top 2💎 Triệu Phú",
        "Top 3🏆 Đại Gia",
        "Top 4🥂 Quý Tộc",
        "Top 5🎩 Quý Nhân",
        "Top 6🌱 Khách Hàng Tiềm Năng",
        "Top 7🌱 Khách Hàng Tiềm Năng",
        "Top 8🌱 Khách Hàng Tiềm Năng",
        "Top 9🌱 Khách Hàng Tiềm Năng",
        "Top 10🌱 Khách Hàng Tiềm Năng",
    ]
    if 1 <= rank <= 10:
        return labels[rank - 1]
    if rank > 10:
        return f"Top {rank}⭐"
    return ""

_VITIEN_DATA: dict = {}

# ==================== BXH TOP 10 (#topbxh / #topbxh_tN) ====================

def _format_topbxh(vitien_data: dict, da_nhan_data: dict, thang: int = 0) -> str:
    from datetime import datetime, date, timedelta

    RANKS = [
        "Top 1👑 Tỷ Phú",
        "Top 2💎 Triệu Phú",
        "Top 3🏆 Đại Gia",
        "Top 4🥂 Quý Tộc",
        "Top 5🎩 Quý Nhân",
        "Top 6🌱 Khách Hàng Tiềm Năng",
        "Top 7🌱 Khách Hàng Tiềm Năng",
        "Top 8🌱 Khách Hàng Tiềm Năng",
        "Top 9🌱 Khách Hàng Tiềm Năng",
        "Top 10🌱 Khách Hàng Tiềm Năng",
    ]

    today = date.today()
    nam_hien_tai = today.year
    results = []

    if thang == 0:
        for sid, v in vitien_data.items():
            co_the_rut = 0.0
            for don in v.get("don_hoan_thanh", []):
                hh = float(don.get("hoa_hong_rong") or don.get("hoa_hong") or 0)
                ngay_str = don.get("ngay_hoan_thanh")
                so_ngay = 0
                if ngay_str:
                    try:
                        ngay_ht = next((datetime.strptime(ngay_str, fmt).date() for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d") if _try_strptime(ngay_str, fmt)), None)
                        so_ngay = (today - ngay_ht).days
                    except Exception:
                        pass
                if so_ngay >= 1:
                    co_the_rut = round(co_the_rut + round((hh * 0.90) * 0.8, 2), 2)

            da_nhan = _get_da_nhan(da_nhan_data, sid, thang=0)
            co_the_rut_hien = max(0.0, round(co_the_rut - da_nhan, 2))
            tong = round(co_the_rut_hien + da_nhan, 2)
            if tong > 0:
                results.append((sid, co_the_rut_hien, da_nhan, tong))

        results.sort(key=lambda x: x[3], reverse=True)
        top10 = results[:10]

        lines = ["🏆 Top 10 BXH   🌷 2026\n"]
        for i, (sid, co_the_rut, da_nhan, tong) in enumerate(top10):
            lines.append(
                f"{RANKS[i]}\n"
                f"  🟢 Có thể rút ngay: {_fmt_money(co_the_rut)}\n"
                f"  💌 Đã nhận: {_fmt_money(da_nhan)}\n"
                f"  💰 Tổng hoa hồng: {_fmt_money(tong)}\n"
                f"─────────────"
            )
        lines.append("💡 Hãy nhắn #vitien để biết bạn đang đứng top bao nhiêu trong BXH nhé!")

    else:
        TEN_THANG = [
            "", "Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4",
            "Tháng 5", "Tháng 6", "Tháng 7", "Tháng 8",
            "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12",
        ]
        if thang < 1 or thang > 12:
            return "❌ Tháng không hợp lệ! Nhắn #topbxh_t1 đến #topbxh_t12 nhé!"

        for sid, v in vitien_data.items():
            tong_thang = 0.0
            for don in v.get("don_hoan_thanh", []):
                hh = float(don.get("hoa_hong_rong") or don.get("hoa_hong") or 0)
                ngay_str = don.get("ngay_hoan_thanh")
                if not ngay_str:
                    continue
                try:
                    ngay_ht = next((datetime.strptime(ngay_str, fmt).date() for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d") if _try_strptime(ngay_str, fmt)), None)
                    so_ngay = (today - ngay_ht).days
                    if so_ngay < 1:
                        continue
                    ngay_co_the_rut = ngay_ht + timedelta(days=3)
                    if ngay_co_the_rut.month == thang and ngay_co_the_rut.year == nam_hien_tai:
                        tong_thang = round(tong_thang + round((hh * 0.90) * 0.8, 2), 2)
                except Exception:
                    pass
            if tong_thang > 0:
                results.append((sid, tong_thang))

        results.sort(key=lambda x: x[1], reverse=True)
        top10 = results[:10]

        if not top10:
            return f"📭 Chưa có dữ liệu hoa hồng có thể rút trong {TEN_THANG[thang]} {nam_hien_tai}."

        lines = [f"🏆 Top 10 BXH   🌷 {TEN_THANG[thang]}\n"]
        for i, (sid, tong_thang) in enumerate(top10):
            lines.append(
                f"{RANKS[i]}\n"
                f"  💰 Tổng hoa hồng: {_fmt_money(tong_thang)}\n"
                f"─────────────"
            )
        lines.append("💡 Hãy nhắn #vitien để biết bạn đang đứng top bao nhiêu trong BXH nhé!")

    return "\n".join(lines)

# ==================== DỮ LIỆU ĐÃ NHẬN ====================

def _load_da_nhan(json_path: str = "da_nhan_by_subid.json") -> dict:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for sid, val in raw.items():
            if isinstance(val, (int, float)):
                result[sid] = {"t0": float(val)}
            elif isinstance(val, dict):
                result[sid] = {k: float(v) for k, v in val.items()}
            else:
                result[sid] = {}
        return result
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.getLogger(__name__).error(f"❌ Lỗi đọc {json_path}: {e}")
        return {}

def _get_da_nhan(da_nhan_data: dict, sid: str, thang: int = 0) -> float:
    entry = da_nhan_data.get(sid, {})
    if not entry:
        return 0.0
    if thang == 0:
        return round(sum(entry.values()), 2)
    return round(entry.get(f"t{thang}", 0.0), 2)

_DA_NHAN_DATA: dict = {}

# [FIX] asyncio.create_task() không giữ strong reference sẽ có thể bị Python
# garbage-collect NGẦM giữa chừng (task "mồ côi") — đặc biệt nguy hiểm với
# task chạy nền có await asyncio.sleep(...) ở đầu như _push_danhan_bg().
# Giữ mọi background task trong set này, tự xoá khi xong, để đảm bảo task
# LUÔN chạy tới cùng dù không ai await nó trực tiếp.
_BACKGROUND_TASKS: set = set()

def _spawn_background_task(coro):
    """Tạo task nền an toàn — không bị mất do garbage collection."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task

# ==================== DỮ LIỆU THÔNG TIN (#thongtin) ====================

THONGTIN_FILE = "thongtin_by_subid.json"

def _load_thongtin(json_path: str = THONGTIN_FILE) -> dict:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.getLogger(__name__).error(f"❌ Lỗi đọc {json_path}: {e}")
        return {}

def _save_thongtin(data: dict, json_path: str = THONGTIN_FILE):
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logging.getLogger(__name__).info(f"💾 Đã ghi {len(data)} bản ghi vào {json_path}")
    except Exception as e:
        logging.getLogger(__name__).error(f"❌ Lỗi ghi {json_path}: {e}")

def _parse_thongtin_info(raw: str) -> str:
    raw = raw.strip()
    lower = raw.lower()
    if lower.startswith("#thongtin_"):
        return raw[len("#thongtin_"):].strip()
    return ""

def _format_thongtin(info_raw: str, zalo_name: str = "") -> str:
    if '\n' in info_raw or any(ord(c) > 127 for c in info_raw):
        return info_raw.strip()
    s = info_raw.strip()
    m = re.match(r'^([a-zA-Z]+)(\d+)$', s)
    if m:
        bank = m.group(1).upper()
        acc  = m.group(2)
        info_line = f"🌷{bank}{acc}"
    else:
        info_line = f"🌷{s.upper()}"
    return f"📩THÔNG TIN CỦA SẾP\n{info_line}"

_THONGTIN_DATA: dict = {}

# ==================== CÀI ĐẶT ====================

GROUP_NAME    = "𝐇𝐨𝐚̀𝐧 🌷 𝐒𝐇𝐎𝐏𝐄𝐄 🛍️"   # ← ĐỔI THÀNH TÊN NHÓM ZALO CỦA BẠN
BOT_NAME      = "Thư"            # Tên Zalo của bot — bỏ qua tin nhắn do chính bot gửi
POLL_INTERVAL = 0.15
PROFILE_DIR   = "./zalo_profile"

# ── Cấu hình Shopee Converter ─────────────────────────────────────────────────
AFFILIATE_ID  = "17332000392"          # affiliate ID cố định
SUB_ID        = DEFAULT_SUB_ID         # "THU"  — lấy từ shopee_converter

MAX_PROCESSED      = 5000
HEARTBEAT_INTERVAL = 3600

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9",
}

# ==================== LOGGING ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ==================== LINK UTILS ====================

SHOPEE_PATTERN = re.compile(
    r"https?://(?:s\.shopee\.vn|vn\.shp\.ee)/[^\s\"<>\)]+")

def extract_shopee_link(text: str) -> str | None:
    match = SHOPEE_PATTERN.search(text)
    return match.group(0).rstrip(".,;!?") if match else None

def is_already_affiliate(url: str) -> bool:
    return (
        "affiliate_id" in url
        or "an_redir" in url
        or "utm_medium=affiliates" in url
        or "shope.ee" in url
    )

def clean_shopee_url(url: str) -> str:
    parsed = urlparse(url)
    if "shopee.vn" not in parsed.netloc:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

# ── Làm sạch tên người gửi để dùng làm sub_id ────────────────────────────────

def clean_sub_id(name: str, fallback: str = SUB_ID) -> str:
    if not name or not name.strip():
        return fallback
    nfkd = _ud.normalize("NFD", name.strip())
    ascii_str = "".join(c for c in nfkd if _ud.category(c) != "Mn")
    ascii_str = ascii_str.replace(" ", "_")
    cleaned = _re.sub(r"[^a-zA-Z0-9_\-]", "", ascii_str)
    cleaned = cleaned[:50].strip("_-")
    return cleaned if cleaned else fallback

# ── Fallback: ghép link thủ công ─────────────────────────────────────────────
def make_affiliate_link_fallback(origin_link: str) -> str:
    clean = clean_shopee_url(origin_link)
    encoded = quote(clean, safe="")
    return (
        f"https://s.shopee.vn/an_redir"
        f"?utm_medium=affiliates"
        f"&affiliate_id={AFFILIATE_ID}"
        f"&sub_id={SUB_ID}"
        f"&origin_link={encoded}"
    )

# ==================== PROCESS LINK (shopee_converter) ====================

async def process_link(raw_link: str, sub_id: str = "") -> str:
    """
    Async wrapper gọi shopee_converter.convert trong thread riêng.
    Thay thế hoàn toàn ExtensionBridge — không cần WebSocket, không cần Chrome thứ 2.
    
    [OPT] Nếu link là short link (s.shopee.vn / vn.shp.ee), resolve redirect
    bằng aiohttp async TRƯỚC (nhanh hơn ~2-3x so với sync urlopen trong thread).
    """
    if shopee_convert is None:
        raise RuntimeError("shopee_converter.py chưa được cài đặt!")

    loop = asyncio.get_running_loop()
    _sub = sub_id or SUB_ID
    _aff = AFFILIATE_ID

    # [OPT] Nếu là short link → resolve async trước để lấy shop_id/product_id ngay
    # tránh blocking thread pool với urlopen
    is_short = bool(re.search(r"(shp\.ee|s\.shopee\.vn)/\w+", raw_link) or "vn.shp.ee" in raw_link)
    if is_short:
        try:
            resolved_url = await asyncio.wait_for(
                _sc_resolve_short_link_async(raw_link), timeout=5.0
            )
            ids = _sc_extract_shop_product(resolved_url)
            if ids:
                shop_id, product_id = ids
                origin   = f"https://shopee.vn/product/{shop_id}/{product_id}"
                encoded  = _urlparse.quote(origin, safe="")
                aff_link = (
                    f"https://s.shopee.vn/an_redir"
                    f"?origin_link={encoded}"
                    f"&affiliate_id={_aff}"
                    f"&sub_id={_sub}"
                )
                log.info(f"✅ [FAST async resolve] shopee_converter: {aff_link}")
                wrapped = await wrap_affiliate_link(aff_link)  # [v28]
                return wrapped
        except Exception as e:
            log.warning(f"⚠️ async resolve thất bại ({e}), fallback sang thread...")

    # Fallback: chạy sync converter trong thread (cho URL dài hoặc khi async fail)
    result = await loop.run_in_executor(
        _executor,
        lambda: shopee_convert(raw=raw_link, affiliate_id=_aff, sub_id=_sub)
    )

    if result.get("success"):
        aff_link = result["affiliate_link"]
        log.info(f"✅ shopee_converter trả về: {aff_link}")
        wrapped = await wrap_affiliate_link(aff_link)  # [v28]
        return wrapped

    raise RuntimeError(result.get("error", "shopee_converter thất bại không rõ nguyên nhân"))

# ==================== COMMISSION CHECK ====================

import aiohttp

COMMISSION_API = "https://data.addlivetag.com/product-data/product-data.php"
_commission_session: aiohttp.ClientSession | None = None

def _get_commission_session() -> aiohttp.ClientSession:
    global _commission_session
    if _commission_session is None or _commission_session.closed:
        _commission_session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=5),
        )
    return _commission_session

def _fmt_vnd(amount) -> str:
    if amount is None:
        return "—"
    return f"{int(amount):,}đ".replace(",", ".")

def _fmt_pct(commission, price) -> str:
    if not price or commission is None:
        return "—"
    return f"{commission / price * 100:.1f}%"

async def _fetch_commission(url: str) -> dict | None:
    session = _get_commission_session()
    async with session.get(COMMISSION_API, params={"url": url}) as resp:
        if resp.status != 200:
            return None
        data = await resp.json(content_type=None)
    if data.get("status") != "success" or "productInfo" not in data:
        return None
    p = data["productInfo"]
    commission = p.get("commission", 0)
    price      = p.get("price", 0)
    return {
        "productName":    (p.get("productName") or "").strip(),
        "commission":     commission,
        "price":          price,
        "commission_str": _fmt_vnd(commission),
        "commission_pct": _fmt_pct(commission, price),
    }

_NO_COMMISSION = object()

async def _sc_resolve_short_link_async(url: str) -> str:
    """
    [OPT] Async HTTP HEAD resolve — nhanh hơn sync urlopen ~2-3x.
    Dùng aiohttp session có sẵn, không mở connection mới.
    Cache kết quả để link lặp lại trả về ngay.
    """
    if url in _resolve_cache:
        return _resolve_cache[url]
    try:
        session = _get_commission_session()
        async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            resolved = str(resp.url)
            _resolve_cache[url] = resolved
            return resolved
    except Exception:
        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(_executor, _sc_resolve_short_link, url)
        _resolve_cache[url] = resolved
        return resolved

async def check_commission(url: str):
    try:
        result = await asyncio.wait_for(_fetch_commission(url), timeout=5.0)  # [OPT] 10s→5s
        if result is None:
            log.info("💖 API không tìm thấy hoa hồng → hiện mẫu không hoa hồng")
            return _NO_COMMISSION
        log.info(
            f"💖 Hoa hồng: {result['commission_str']} "
            f"({result['commission_pct']})  —  {result['productName'][:40]}"
        )
        return result
    except asyncio.TimeoutError:
        log.warning("⚠️ check_commission timeout 5s → hiện mẫu không hoa hồng")
        return _NO_COMMISSION
    except Exception as e:
        log.warning(f"⚠️ check_commission lỗi: {e} → hiện mẫu không hoa hồng")
        return _NO_COMMISSION

# ==================== JS OBSERVER ====================

_JS_SETUP_OBSERVER = r"""
() => {
    window.__botQueue = window.__botQueue || [];
    window.__botNodeMap = window.__botNodeMap || {};
    window.__botSeenIds = window.__botSeenIds || new Set();
    window.__botJoinQueue = window.__botJoinQueue || [];
    if (window.__botObserver) window.__botObserver.disconnect();
    if (window.__botWatchdog) clearInterval(window.__botWatchdog);

    const linkRe = /https?:\/\/(s\.shopee\.vn|vn\.shp\.ee)\/[^\s"<\)\]]+/g;

    // Link cần chuyển hướng sang Thư (TikTok, Lazada, ShopeeFood, SPF)
    const redirectLinkRe = /https?:\/\/((?:www\.)?tiktok\.com|(?:vt\.)?tiktok\.com|vm\.tiktok\.com|(?:www\.)?lazada\.vn|s\.lazada\.vn|shopeefood\.shopee\.vn\/u\/|spf\.shopee\.vn\/)[^\s"<\)\]]*/g;
    const REDIRECT_EXACT = [
        'https://shopeefood.shopee.vn/u/aUmNQES',
        'https://spf.shopee.vn/8fQI4TUWqy',
    ];
    function isRedirectLink(txt) {
        // Kiểm tra các link cố định trước
        for (const exact of REDIRECT_EXACT) {
            if (txt.includes(exact)) return true;
        }
        // Kiểm tra regex TikTok/Lazada/ShopeeFood/SPF
        redirectLinkRe.lastIndex = 0;
        const hasMatch = redirectLinkRe.test(txt);
        redirectLinkRe.lastIndex = 0;
        return hasMatch;
    }
    function extractRedirectLinks(txt) {
        const results = [];
        redirectLinkRe.lastIndex = 0;
        let m;
        while ((m = redirectLinkRe.exec(txt)) !== null) results.push(m[0]);
        redirectLinkRe.lastIndex = 0;
        for (const exact of REDIRECT_EXACT) {
            if (txt.includes(exact) && !results.includes(exact)) results.push(exact);
        }
        return results;
    }

    const SUPPORTED_COMMANDS = ['#donhang', '#vitien', '#ruttien', '#thongtin', '#huongdan', '#my_id'];
    const DONHANG_PAGE_RE = /^#donhang([2-9]|[1-9]\d+)$/i;
    const RUTTIEN_NUM_RE  = /#ruttien_([\d.,]+)(\s|$)/i;
    const THONGTIN_RE     = /#thongtin_\S+/i;
    const ADMIN_RE        = /#admin/i;  // Không bao giờ xử lý lệnh này

    // Tên người gửi KHÔNG đọc từ DOM — luôn trả về chuỗi rỗng
    function getSenderFromNode(node) { return ''; }
    function getMsgIdFromNode(node) {
        // data-qid format: "TIMESTAMP@GROUP_ID_USER_ID_GROUP_ID"
        // msgId  = phần trước '_' đầu tiên: "TIMESTAMP@GROUP_ID"
        // userId = phần thứ 2 sau '_': USER_ID (Zalo ID của người gửi)

        function parseQid(qid) {
            const parts = qid.split('_');
            return { msgId: parts[0] || qid, userId: parts.length >= 2 ? parts[1] : '' };
        }

        // 1. Tìm trong chính node và các con (ưu tiên node nhỏ nhất có data-qid)
        if (node.querySelectorAll) {
            const inner = node.querySelectorAll('[data-qid]');
            if (inner.length > 0) {
                return parseQid(inner[0].getAttribute('data-qid'));
            }
        }
        const selfQid = node.getAttribute && node.getAttribute('data-qid');
        if (selfQid) return parseQid(selfQid);

        // 2. Leo lên DOM tối đa 30 cấp
        let el = node.parentElement;
        for (let i = 0; i < 30; i++) {
            if (!el || el === document.body) break;
            const qid = el.getAttribute && el.getAttribute('data-qid');
            if (qid) return parseQid(qid);
            // Cũng thử tìm sibling/con gần nhất có data-qid trong parent
            if (el.querySelectorAll) {
                const found = el.querySelectorAll('[data-qid]');
                if (found.length > 0) return parseQid(found[0].getAttribute('data-qid'));
            }
            el = el.parentElement;
        }
        return { msgId: '', userId: '' };
    }

    function extractLinksFromNode(node) {
        const results = [];
        const txt = (node.innerText || node.textContent || '');
        linkRe.lastIndex = 0;
        let m;
        while ((m = linkRe.exec(txt)) !== null) results.push(m[0]);
        linkRe.lastIndex = 0;
        if (node.querySelectorAll) {
            node.querySelectorAll('a[href]').forEach(a => {
                linkRe.lastIndex = 0;
                if (linkRe.test(a.href)) results.push(a.href);
                linkRe.lastIndex = 0;
            });
        }
        return [...new Set(results)];
    }

    const containerSels = [
        '[class*="message-view__scroll__inner"]',
        '[class*="message-view__scroll"]',
        '[class*="message-view"]',
        '[class*="threadChat"]',
        '[class*="chat-content"]',
    ];
    function getContainer() {
        for (const s of containerSels) {
            const c = document.querySelector(s);
            if (c) return c;
        }
        return document.body;
    }

    const observer = new MutationObserver((mutations) => {
        for (const mut of mutations) {
            for (const node of mut.addedNodes) {
                if (node.nodeType !== 1) continue;
                const links = extractLinksFromNode(node);
                const nodeText = (node.innerText || node.textContent || '').trim().slice(0, 300);

                if (nodeText && /đã được thêm vào nhóm/i.test(nodeText)) continue;

                // ── #admin — bỏ qua hoàn toàn, không đưa vào queue ───────────
                if (nodeText && ADMIN_RE.test(nodeText)) continue;

                // ── Kiểm tra lệnh text ────────────────────────────────────────
                if (nodeText) {
                    const lowerText = nodeText.toLowerCase().trim();

                    // #donhang2, #donhang3, ... (ưu tiên trước #donhang)
                    const donhangPageM = nodeText.match(/#donhang([2-9]|[1-9]\d+)(\s|$)/i);
                    if (donhangPageM) {
                        const pageNum = donhangPageM[1];
                        const seenKey = nodeText.slice(0, 200);
                        if (!window.__botSeenIds.has(seenKey)) {
                            window.__botSeenIds.add(seenKey);
                            const nodeId = Date.now() + '_cmd_' + Math.random().toString(36).slice(2, 8);
                            window.__botNodeMap[nodeId] = node;
                            const _qid1 = getMsgIdFromNode(node);
                            window.__botQueue.push({
                                command: '#donhang' + pageNum,
                                sender: getSenderFromNode(node),
                                nodeId: nodeId,
                                msgId: _qid1.msgId,
                                userId: _qid1.userId,
                            });
                        }
                        continue;
                    }

                    // #ruttien_<số>
                    const ruttienNumM = RUTTIEN_NUM_RE.exec(nodeText);
                    if (ruttienNumM) {
                        const seenKey = nodeText.slice(0, 200);
                        if (!window.__botSeenIds.has(seenKey)) {
                            window.__botSeenIds.add(seenKey);
                            const nodeId = Date.now() + '_cmd_' + Math.random().toString(36).slice(2, 8);
                            window.__botNodeMap[nodeId] = node;
                            const _qid2 = getMsgIdFromNode(node);
                            window.__botQueue.push({
                                command: '#ruttien_' + ruttienNumM[1],
                                sender: getSenderFromNode(node),
                                nodeId: nodeId,
                                msgId: _qid2.msgId,
                                userId: _qid2.userId,
                            });
                        }
                        continue;
                    }

                    // #thongtin_<bank><số>
                    const thongtinM = THONGTIN_RE.exec(nodeText);
                    if (thongtinM) {
                        const seenKey = nodeText.slice(0, 200);
                        if (!window.__botSeenIds.has(seenKey)) {
                            window.__botSeenIds.add(seenKey);
                            const nodeId = Date.now() + '_cmd_' + Math.random().toString(36).slice(2, 8);
                            window.__botNodeMap[nodeId] = node;
                            const _qid4 = getMsgIdFromNode(node);
                            window.__botQueue.push({
                                command: thongtinM[0].trim(),
                                sender: getSenderFromNode(node),
                                nodeId: nodeId,
                                msgId: _qid4.msgId,
                                userId: _qid4.userId,
                            });
                        }
                        continue;
                    }

                    // Các lệnh đơn giản
                    for (const cmd of SUPPORTED_COMMANDS) {
                        if (lowerText.startsWith(cmd) || lowerText.includes('\n' + cmd) || lowerText === cmd) {
                            const seenKey = nodeText.slice(0, 200);
                            if (!window.__botSeenIds.has(seenKey)) {
                                window.__botSeenIds.add(seenKey);
                                const nodeId = Date.now() + '_cmd_' + Math.random().toString(36).slice(2, 8);
                                window.__botNodeMap[nodeId] = node;
                                const _qid5 = getMsgIdFromNode(node);
                                window.__botQueue.push({
                                    command: cmd,
                                    sender: getSenderFromNode(node),
                                    nodeId: nodeId,
                                    msgId: _qid5.msgId,
                                    userId: _qid5.userId,
                                });
                            }
                            break;
                        }
                    }
                }

                // ── Xử lý link cần chuyển hướng (TikTok / Lazada / ShopeeFood / SPF) ──
                if (isRedirectLink(nodeText)) {
                    const rdLinks = extractRedirectLinks(nodeText);
                    if (rdLinks.length > 0) {
                        const _qidR = getMsgIdFromNode(node);
                        const seenKeyR = _qidR.msgId || nodeText.slice(0, 200);
                        if (!window.__botSeenIds.has(seenKeyR)) {
                            window.__botSeenIds.add(seenKeyR);
                            const nodeIdR = Date.now() + '_rd_' + Math.random().toString(36).slice(2, 8);
                            window.__botNodeMap[nodeIdR] = node;
                            window.__botQueue.push({
                                redirect_link: rdLinks[0],
                                sender: getSenderFromNode(node),
                                nodeId: nodeIdR,
                                msgId: _qidR.msgId,
                                userId: _qidR.userId,
                            });
                        }
                        continue;
                    }
                }

                // ── Xử lý link Shopee ─────────────────────────────────────────
                if (links.length === 0) continue;
                const _qid6 = getMsgIdFromNode(node);
                const seenKey = _qid6.msgId || nodeText.slice(0, 200);
                if (window.__botSeenIds.has(seenKey)) continue;
                window.__botSeenIds.add(seenKey);

                const nodeId = Date.now() + '_' + Math.random().toString(36).slice(2, 8);
                window.__botNodeMap[nodeId] = node;
                for (const lnk of links) {
                    window.__botQueue.push({
                        link: lnk,
                        sender: getSenderFromNode(node),
                        nodeId: nodeId,
                        msgId: _qid6.msgId,
                        userId: _qid6.userId,
                    });
                }
            }
        }
    });

    const container = getContainer();
    observer.observe(container, { childList: true, subtree: true });
    window.__botObserver = observer;
    window.__botObserverTarget = container;

    // Watchdog: re-attach nếu container DOM bị thay thế (Zalo re-render)
    window.__botWatchdog = setInterval(() => {
        const newContainer = getContainer();
        if (newContainer !== window.__botObserverTarget) {
            observer.disconnect();
            observer.observe(newContainer, { childList: true, subtree: true });
            window.__botObserverTarget = newContainer;
            console.log('[BotV25] Observer re-attached to new container');
        }
    }, 2000);

    console.log('[BotV25] Observer + Watchdog started');
    return true;
}
"""

_JS_DRAIN_QUEUE = """
() => {
    const items = window.__botQueue || [];
    window.__botQueue = [];
    const joins = window.__botJoinQueue || [];
    window.__botJoinQueue = [];
    if (items.length > 0) console.log('[BotV25] Drain', items.length, 'items');
    return { items, joins };
}
"""

_JS_CHECK_OBSERVER = """
() => {
    const target = window.__botObserverTarget;
    const containerSels = [
        '[class*="message-view__scroll__inner"]','[class*="message-view__scroll"]',
        '[class*="message-view"]','[class*="threadChat"]','[class*="chat-content"]',
    ];
    let container = null;
    let containerClass = '';
    for (const s of containerSels) {
        const c = document.querySelector(s);
        if (c) { container = c; containerClass = c.className || c.tagName; break; }
    }
    if (!container) { container = document.body; containerClass = 'body'; }
    return {
        hasObserver: !!window.__botObserver,
        hasWatchdog: !!window.__botWatchdog,
        targetMatch: target === container,
        containerClass: containerClass,
        queueLen: (window.__botQueue || []).length,
    };
}
"""

_JS_PURGE_LINK = """
(link) => {
    // Xóa item chứa link này khỏi queue (không xóa seenIds để tránh unblock link hợp lệ)
    if (window.__botQueue) {
        window.__botQueue = window.__botQueue.filter(item => !item.link || !item.link.includes(link));
    }
}
"""

# ==================== BOT ZALO ====================

class ZaloAffiliateBot:

    def __init__(self):
        self._playwright     = None
        self._context        = None
        self._zalo_page      = None
        self._pinned_group_title = ""
        self._pinned_group_id    = ""
        self.processed_node_ids  = set()
        self.link_senders        = {}
        self._processing_links   = set()
        self._send_btn_sel       = None

    async def start(self):
        await self._open_zalo()
        await self._listen_loop()

    async def _open_zalo(self):
        log.info("Đang mở Zalo Web...")
        Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1280,800",
                # ── Background / throttle ────────────────────────────────
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-background-media-suspend",
                "--disable-hang-monitor",
                "--disable-features=OptimizeBackgroundRendering,BackForwardCache",
                # ── VPS / RDP 24/24 ──────────────────────────────────────
                "--use-gl=swiftshader",
                "--use-angle=swiftshader",
                "--disable-gpu-sandbox",
                "--disable-software-rasterizer",
                "--ignore-gpu-blocklist",
                "--hide-scrollbars",
                "--mute-audio",
                "--force-device-scale-factor=1",
            ],
            viewport={"width": 1280, "height": 800},
            user_agent=HEADERS["User-Agent"],
            locale="vi-VN",
            permissions=["clipboard-read", "clipboard-write"],
        )
        self._zalo_page = await self._context.new_page()
        await self._zalo_page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        await self._zalo_page.goto("https://chat.zalo.me", wait_until="domcontentloaded")
        await self._zalo_page.wait_for_timeout(2000)

        url = self._zalo_page.url
        if "login" in url or len(url) < 25:
            log.info(">>> Vui lòng đăng nhập Zalo Web (quét QR) rồi nhấn Enter <<<")
            input()
        else:
            log.info("✅ Zalo đã có session.")

        await self._find_group()

    async def _get_current_group_id(self) -> str:
        try:
            group_id = await self._zalo_page.evaluate("""
                () => {
                    const url = window.location.href;
                    const patterns = [
                        /[?&]g=([a-z0-9]+)/i,
                        /[?&]conversation_id=([^&]+)/,
                        /[?&]convId=([^&]+)/,
                        /[?&]groupId=([^&]+)/,
                        /[?&]threadId=([^&]+)/,
                    ];
                    for (const re of patterns) {
                        const m = url.match(re);
                        if (m && m[1] && m[1].length >= 5) return m[1];
                    }
                    // Fallback DOM
                    const domSels = [
                        '[data-conversation-id]','[data-conv-id]',
                        '[data-group-id]','[data-thread-id]','[data-id]',
                    ];
                    for (const s of domSels) {
                        const el = document.querySelector(s);
                        if (el) {
                            const id = el.dataset.conversationId || el.dataset.convId
                                    || el.dataset.groupId || el.dataset.threadId || el.dataset.id;
                            if (id && id.length >= 5) return id;
                        }
                    }
                    // Active conv-item
                    const activeItem = document.querySelector(
                        '[class*="conv-item"][class*="active"],[class*="ConvItem"][class*="active"],' +
                        '[class*="conv-item"][class*="selected"],[class*="conversation-item"][class*="active"]'
                    );
                    if (activeItem) {
                        for (const attr of activeItem.getAttributeNames()) {
                            const val = activeItem.getAttribute(attr);
                            if (val && /^[a-z0-9]{8,}$/i.test(val)) return val;
                        }
                    }
                    return '';
                }
            """)
            return (group_id or "").strip()
        except Exception as e:
            log.warning(f"⚠️ Không lấy được group ID: {e}")
            return ""

    async def _find_group(self):
        import unicodedata
        def norm(s): return unicodedata.normalize("NFC", s or "").strip().lower()
        log.info(f"Đang tìm nhóm: '{GROUP_NAME}'")
        await self._zalo_page.wait_for_timeout(1500)
        for sel in ["div[class*='conv-item']", "div[class*='ConvItem']",
                    "div[class*='conversation']", "div[class*='chat-item']"]:
            for item in await self._zalo_page.locator(sel).all():
                try:
                    txt = await item.inner_text()
                    if norm(GROUP_NAME) in norm(txt):
                        await item.click()
                        await self._zalo_page.wait_for_timeout(1000)
                        first_line = txt.strip().splitlines()[0].strip()
                        self._pinned_group_title = first_line or GROUP_NAME
                        self._pinned_group_id = await self._get_current_group_id()
                        log.info(f"✅ Đã vào nhóm: {self._pinned_group_title} (ID={self._pinned_group_id or 'không lấy được'})")
                        log.info(
                            f"📌 NHÓM ĐÃ GHIM — Bot chỉ gửi tin vào nhóm này.\n"
                            f"   ⚠️  NGHIÊM CẤM bot tự chuyển nhóm dù bất kỳ lý do gì.\n"
                            f"   ⚠️  Nếu bạn click sang nhóm/chat khác → bot sẽ HỦY gửi tin.\n"
                            f"   ⚠️  Muốn đổi nhóm → dừng bot (Ctrl+C) rồi chạy lại."
                        )
                        await self._mark_existing_links()
                        return
                except Exception:
                    pass
        log.warning(f">>> Không tìm thấy nhóm '{GROUP_NAME}' — vui lòng click thủ công vào nhóm rồi nhấn Enter <<<")
        input()
        self._pinned_group_title = GROUP_NAME
        self._pinned_group_id = await self._get_current_group_id()
        await self._mark_existing_links()

    async def _mark_existing_links(self):
        try:
            result = await self._zalo_page.evaluate(r"""
                () => {
                    const re = /https?:\/\/(s\.shopee\.vn|vn\.shp\.ee)\/[^\s"<\)\]]+/g;
                    const sels = [
                        '[class*="message-view__scroll__inner"]',
                        '[class*="message-view__scroll"]',
                        '[class*="message-view"]',
                        '[class*="threadChat"]',
                        '[class*="chat-content"]',
                    ];
                    window.__botSeenIds = window.__botSeenIds || new Set();
                    const ids = [];
                    for (const sel of sels) {
                        const c = document.querySelector(sel);
                        if (!c) continue;
                        c.querySelectorAll('[class*="message"],[class*="bubble"]').forEach(node => {
                            re.lastIndex = 0;
                            const txt = (node.innerText || node.textContent || '').trim();
                            if (re.test(txt)) {
                                const key = txt.slice(0, 200);
                                if (key) { window.__botSeenIds.add(key); ids.push(key); }
                            }
                        });
                        if (ids.length > 0) return ids;
                    }
                    return ids;
                }
            """)
            log.info(f"✅ Đã ghim {len(result or [])} tin nhắn CŨ vào seenIds.")
        except Exception as e:
            log.warning(f"Lỗi mark existing links: {e}")

    # ── Observer ──────────────────────────────────────────────────────────────

    async def _setup_observer(self):
        await self._zalo_page.evaluate(_JS_SETUP_OBSERVER)
        log.info("✅ MutationObserver sẵn sàng.")

    # ── Vòng lặp chính ────────────────────────────────────────────────────────

    async def _listen_loop(self):
        log.info("🤖 Bot đang lắng nghe (real-time)...")
        await self._setup_observer()
        errors = 0
        last_heartbeat = asyncio.get_running_loop().time()
        last_obs_check = asyncio.get_running_loop().time()

        while True:
            try:
                now = asyncio.get_running_loop().time()

                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    log.info(f"💓 Bot vẫn đang chạy | processed_nodes={len(self.processed_node_ids)}")
                    last_heartbeat = now

                if now - last_obs_check >= 30:
                    last_obs_check = now
                    try:
                        status = await self._zalo_page.evaluate(_JS_CHECK_OBSERVER)
                        if not status.get("hasObserver") or not status.get("targetMatch"):
                            log.warning(f"⚠️ Observer mất target — re-attach... (status={status})")
                            await self._setup_observer()
                        else:
                            log.info(f"👁 Observer OK | container={status.get('containerClass','?')} | queue={status.get('queueLen',0)}")
                    except Exception as obs_err:
                        log.warning(f"⚠️ Lỗi check observer: {obs_err} — re-setup...")
                        await self._setup_observer()

                raw_result = await self._zalo_page.evaluate(_JS_DRAIN_QUEUE)
                errors = 0

                # Tách items và join events từ kết quả drain
                join_events = raw_result.get("joins", []) if isinstance(raw_result, dict) else []
                items       = raw_result.get("items", []) if isinstance(raw_result, dict) else (raw_result or [])

                await self._scroll_to_bottom()

                new_items    = []
                new_commands = []
                for item in items:
                    raw     = item.get("link", "")
                    name    = (item.get("sender") or "").strip()
                    node_id = (item.get("nodeId") or "").strip()
                    command = (item.get("command") or "").strip()
                    msg_id  = (item.get("msgId") or "").strip()
                    user_id = (item.get("userId") or "").strip()
                    # Luôn ưu tiên userId (Zalo ID từ data-qid) làm sub_id để tra cứu đơn hàng/ví
                    if user_id:
                        name = user_id
                    elif not name:
                        name = ""

                    if command:
                        if name and name.strip().lower() == BOT_NAME.lower():
                            continue
                        new_commands.append((command, name or "", node_id, msg_id))
                        continue

                    # ── Link cần chuyển hướng sang Thư ────────────────
                    redirect_link = item.get("redirect_link", "")
                    if redirect_link:
                        if name and name.strip().lower() == BOT_NAME.lower():
                            log.info(f"⏭ Bỏ qua redirect_link của bot: {redirect_link}")
                            continue
                        log.info(f"🔀 Phát hiện redirect link từ [{name or '???'}]: {redirect_link}")
                        await self._handle_redirect_link(redirect_link, name, node_id, msg_id)
                        continue

                    link = extract_shopee_link(raw)
                    if not link:
                        continue
                    if is_already_affiliate(link):
                        continue
                    if name and name.strip().lower() == BOT_NAME.lower():
                        log.info(f"⏭ Bỏ qua tin nhắn của bot ({BOT_NAME}): {link}")
                        continue
                    if any(link in p or p in link for p in self._processing_links):
                        log.info(f"⏭ Bỏ qua link đang xử lý: {link}")
                        continue
                    if len(self.processed_node_ids) > MAX_PROCESSED:
                        self.processed_node_ids = set(list(self.processed_node_ids)[-2000:])
                    sender = name or self.link_senders.get(link, "")
                    new_items.append((link, sender, node_id, msg_id))

                new_items.sort(key=lambda x: x[2])

                for cmd, sender, node_id, msg_id in new_commands:
                    log.info(f"📩 Lệnh [{cmd}] từ [{sender or '???'}] msg_id=[{msg_id or '-'}]")
                    await self._handle_command(cmd, sender, msg_id)

                # Nhóm các link cùng node_id (cùng 1 tin nhắn) lại với nhau
                from collections import OrderedDict
                grouped: OrderedDict = OrderedDict()
                for link, sender, node_id, msg_id in new_items:
                    group_key = node_id or msg_id or link
                    if group_key not in grouped:
                        grouped[group_key] = {"links": [], "sender": sender, "node_id": node_id, "msg_id": msg_id}
                    if link not in [l for l in grouped[group_key]["links"]]:
                        grouped[group_key]["links"].append(link)

                for group_key, group in grouped.items():
                    links   = group["links"]
                    sender  = group["sender"]
                    node_id = group["node_id"]
                    msg_id  = group["msg_id"]
                    log.info(f"🎯 Nhóm {len(links)} link từ [{sender or '???'}] node={node_id}: {links}")
                    if len(links) == 1:
                        await self._handle(links[0], sender, node_id, msg_id)
                    else:
                        await self._handle_multi(links, sender, node_id, msg_id)

            except Exception as e:
                errors += 1
                err_str = str(e)
                log.error(f"Lỗi polling ({errors}): {e}")
                if errors >= 3 or "Timeout" in err_str or "timeout" in err_str or "Target page" in err_str:
                    log.warning("⚠️ Phát hiện lỗi — re-setup observer...")
                    try:
                        await self._setup_observer()
                        errors = 0
                    except Exception as obs_err:
                        log.error(f"❌ Re-setup observer thất bại: {obs_err}")
                        await asyncio.sleep(5)

            await asyncio.sleep(POLL_INTERVAL)

    # ── Chuyển hướng link TikTok / Lazada / ShopeeFood / SPF ─────────────────

    _REDIRECT_REPLY_MSG = "Vui lòng tag @Thư hoặc ib/call riêng để được chuyển link nhanh nhất nhé"

    async def _handle_redirect_link(self, link: str, sender_name: str, node_id: str, msg_id: str):
        """Khi khách gửi link TikTok/Lazada/ShopeeFood/SPF → trả lời tin nhắn cố định."""
        page = self._zalo_page
        log.info(f"🔀 _handle_redirect_link: {link} | sender={sender_name or '???'} | msg_id={msg_id or '-'}")
        try:
            await self._send_text_reply(page, self._REDIRECT_REPLY_MSG, sender_name, with_mention=False, msg_id=msg_id)
            log.info(f"✅ Đã gửi hướng dẫn chuyển link cho [{sender_name or '???'}]")
        except Exception as e:
            log.error(f"❌ Lỗi gửi redirect reply: {e}")

    # ── Xử lý lệnh text ───────────────────────────────────────────────────────

    # Selector thực tế nút Trả lời Zalo — xác nhận qua DevTools:
    # DOM: <i class="fa fa-Quote_24_Filled quote-sign"> bên trong wrapper MSABtn-btn
    _REPLY_BTN_SELECTORS = [
        'i[class*="Quote_24_Filled"]',   # ✅ class thực tế
        'i[class*="quote-sign"]',         # ✅ class thực tế
        'i[class*="Quote"][class*="Filled"]',
        '[class*="MSABtn-btn"][class*="reply"]',
        '[class*="MSABtn-btn"][class*="Reply"]',
        '[data-translate-title="STR_REPLY_MSG"]',  # fallback cũ
        '[title="Trả lời"]',
    ]

    async def _find_and_click_reply_btn(self, page, hover_rect: dict, msg_id_hint: str = "") -> bool:
        """
        Đã hover vào bubble → chờ nút Reply (i.fa-Quote_24_Filled) xuất hiện
        → click wrapper cha gần tọa độ hover nhất.
        """
        sel_joined = ", ".join(self._REPLY_BTN_SELECTORS)
        try:
            await page.wait_for_selector(sel_joined, state="visible", timeout=300)  # [OPT] 500→300ms
        except Exception:
            pass

        clicked = await page.evaluate(
            """(args) => {
                const hx = args.hx, hy = args.hy, sels = args.sels;
                let btns = [];
                for (const s of sels) {
                    const found = [...document.querySelectorAll(s)].filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    btns = btns.concat(found);
                }
                if (btns.length === 0) return false;
                let best = null, bestDist = Infinity;
                for (const btn of btns) {
                    const r = btn.getBoundingClientRect();
                    const dist = Math.sqrt((r.left+r.width/2-hx)**2 + (r.top+r.height/2-hy)**2);
                    if (dist < bestDist) { bestDist = dist; best = btn; }
                }
                if (!best) return false;
                // Icon không clickable trực tiếp → click wrapper cha
                const target = best.closest('button,[role="button"],[class*="MSABtn-btn"]') || best;
                target.click();
                return true;
            }""",
            {"hx": hover_rect["x"], "hy": hover_rect["y"], "sels": self._REPLY_BTN_SELECTORS}
        )
        return bool(clicked)

    async def _scroll_element_into_view(self, page, msg_id: str) -> dict | None:
        """
        FIX nguyên nhân 4: cuộn bubble vào viewport nếu bị khuất, rồi trả rect.
        Trả về rect {x, y, w, h} của bubble text (không phải wrapper), hoặc None.
        """
        return await page.evaluate(f"""
            () => {{
                const prefix = {repr(msg_id + '_')};
                const exact  = {repr(msg_id)};

                // Tìm element có data-qid khớp
                let wrapper = null;
                for (const el of document.querySelectorAll('[data-qid]')) {{
                    const q = el.getAttribute('data-qid') || '';
                    if (q === exact || q.startsWith(prefix)) {{
                        wrapper = el; break;
                    }}
                }}
                if (!wrapper) return null;

                // FIX nguyên nhân 1: tìm bubble text con (element nhỏ nhất chứa nội dung)
                // Zalo thường có cấu trúc: wrapper[data-qid] > ... > div.chat-message > div.bubble
                const bubbleSels = [
                    '[class*="chat-message__body"]', '[class*="MQA-content"]',
                    '[class*="message-text"]',        '[class*="bubble-content"]',
                    '[class*="msg-content"]',         '[class*="chatBubble"]',
                    '[class*="zmsg-text"]',
                ];
                let bubble = null;
                for (const s of bubbleSels) {{
                    const b = wrapper.querySelector(s);
                    if (b) {{ bubble = b; break; }}
                }}
                // Fallback: dùng wrapper nhưng chọn con text trực tiếp nhất
                if (!bubble) {{
                    // Lấy leaf element có text
                    const walker = document.createTreeWalker(wrapper, NodeFilter.SHOW_ELEMENT);
                    let node, smallest = wrapper;
                    while ((node = walker.nextNode())) {{
                        if ((node.innerText || '').trim().length > 0) {{
                            const r = node.getBoundingClientRect();
                            const sr = smallest.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.width * r.height < sr.width * sr.height)
                                smallest = node;
                        }}
                    }}
                    bubble = smallest;
                }}

                // Cuộn bubble vào viewport nếu bị khuất
                const r0 = bubble.getBoundingClientRect();
                const vh = window.innerHeight;
                if (r0.top < 0 || r0.bottom > vh) {{
                    bubble.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                }}

                const r = bubble.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return null;
                // Hover vào 1/3 trên của bubble (nút Reply xuất hiện góc trên-phải)
                return {{ x: r.left + r.width * 0.75, y: r.top + r.height * 0.3,
                          w: r.width, h: r.height }};
            }}
        """)

    async def _reply_to_message_id(self, page, msg_id: str) -> bool:
        """
        Tìm bubble theo data-qid → cuộn vào viewport → hover đúng vùng bubble text
        → chờ nút Reply xuất hiện → click nút gần nhất.
        Retry 3 lần.
        """
        if not msg_id:
            return False

        for attempt in range(3):
            try:
                # FIX 4: cuộn vào viewport + FIX 1: lấy rect của bubble text, không phải wrapper
                rect = await self._scroll_element_into_view(page, msg_id)

                if not rect:
                    log.warning(f"⚠️ [attempt {attempt+1}] bubble not found/visible: {msg_id}")
                    await asyncio.sleep(0.4)
                    continue

                # Hover vào góc trên-phải của bubble (nơi nút Reply xuất hiện)
                await page.mouse.move(rect["x"], rect["y"])
                await asyncio.sleep(0.08)  # [OPT] 0.15s→0.08s, đủ trigger hover

                # FIX 2+3: chờ nút xuất hiện rồi click nút gần nhất
                clicked = await self._find_and_click_reply_btn(page, rect, msg_id)
                if clicked:
                    await asyncio.sleep(0.08)  # [OPT] 0.15s→0.08s
                    log.info(f"✅ Reply clicked attempt={attempt+1} msgId={msg_id}")
                    return True

                log.warning(f"⚠️ [attempt {attempt+1}] Reply button not visible after hover — retry...")
                # Di chuyển chuột ra ngoài rồi thử lại (reset hover state)
                await page.mouse.move(rect["x"] + 200, rect["y"])
                await asyncio.sleep(0.1)  # [OPT] 0.2s→0.1s

            except Exception as e:
                log.warning(f"⚠️ [attempt {attempt+1}] _reply_to_message_id lỗi: {e}")
                await asyncio.sleep(0.3)  # [OPT] 0.4s→0.3s

        log.error(f"❌ _reply_to_message_id thất bại sau 3 lần (msgId={msg_id})")
        return False

    async def _reply_to_message_by_link(self, page, link: str) -> bool:
        """
        Fallback: Tìm bubble chứa link Shopee → cuộn vào viewport → hover → click Reply.
        Dùng khi msg_id không lấy được từ DOM.
        """
        try:
            rect = await page.evaluate(f"""
                () => {{
                    const linkSnippet = {repr(link[:50])};

                    // Tìm bubble nhỏ nhất chứa link (ưu tiên element có data-qid)
                    let found = null;
                    let foundArea = Infinity;

                    // Ưu tiên: tìm trong element có data-qid
                    for (const el of document.querySelectorAll('[data-qid]')) {{
                        const txt = (el.innerText || el.textContent || '');
                        if (!txt.includes(linkSnippet)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (r.width * r.height < foundArea) {{
                            found = el; foundArea = r.width * r.height;
                        }}
                    }}

                    // Fallback: tìm trong các class message/bubble
                    if (!found) {{
                        const msgSels = [
                            '[class*="chat-message"]', '[class*="MsgItem"]',
                            '[class*="message-item"]', '[class*="bubble"]',
                        ];
                        for (const sel of msgSels) {{
                            for (const el of document.querySelectorAll(sel)) {{
                                const txt = (el.innerText || el.textContent || '');
                                if (!txt.includes(linkSnippet)) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width === 0 || r.height === 0) continue;
                                if (r.width * r.height < foundArea) {{
                                    found = el; foundArea = r.width * r.height;
                                }}
                            }}
                            if (found) break;
                        }}
                    }}

                    if (!found) return null;

                    // Cuộn vào viewport nếu bị khuất
                    const r0 = found.getBoundingClientRect();
                    if (r0.top < 0 || r0.bottom > window.innerHeight) {{
                        found.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                    }}

                    const r = found.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return null;
                    // Hover vào góc trên-phải (nơi nút Reply xuất hiện)
                    return {{ x: r.left + r.width * 0.75, y: r.top + r.height * 0.3,
                              w: r.width, h: r.height }};
                }}
            """)

            if not rect:
                log.warning("⚠️ _reply_to_message_by_link: không tìm thấy bubble chứa link")
                return False

            await page.mouse.move(rect["x"], rect["y"])
            await asyncio.sleep(0.1)  # [OPT] 0.25s→0.1s

            clicked = await self._find_and_click_reply_btn(page, rect)
            if clicked:
                await asyncio.sleep(0.1)  # [OPT] 0.3s→0.1s
                log.info("✅ Reply by link fallback clicked")
                return True

            log.warning("⚠️ _reply_to_message_by_link: hover ok nhưng không thấy nút Reply")
            return False

        except Exception as e:
            log.warning(f"⚠️ _reply_to_message_by_link lỗi: {e}")
            return False

    async def _send_text_reply(self, page, reply_text: str, sender_name: str = "", with_mention: bool = True, msg_id: str = ""):
        """Helper dùng chung: ưu tiên reply theo msg_id, fallback về @mention."""
        await self._ensure_correct_group()
        await self._scroll_to_bottom()

        # Thử reply theo ID tin nhắn trước
        replied = False
        if msg_id:
            replied = await self._reply_to_message_id(page, msg_id)

        await self._click_chat_box(page)
        await asyncio.sleep(0.03)

        # @mention đã bị loại bỏ — không đọc tên từ DOM

        await page.evaluate("(t) => navigator.clipboard.writeText(t)", reply_text)
        await page.keyboard.press("Control+v")

        # Đóng quote banner (nút X) trước khi gửi — giống flow xử lý link Shopee
        await asyncio.sleep(0.05)  # [OPT] 0.08s→0.05s
        if replied:
            closed = await self._click_close_btn(page)
            log.info(f"{'✅ Đóng quote banner (lệnh #)' if closed else '⚠️ Không tìm thấy quote banner (lệnh #)'}")
        await asyncio.sleep(0.05)  # [OPT] 0.08s→0.05s
        await self._ensure_correct_group()
        await self._click_send_button(page)

    async def _handle_command(self, command: str, sender_name: str, msg_id: str = ""):
        page = self._zalo_page
        cmd_lower = command.strip().lower()

        # ── #admin — bỏ qua hoàn toàn, không trả lời ─────────────────────────
        if "#admin" in cmd_lower:
            log.info(f"⛔ Phát hiện #admin từ [{sender_name or '???'}] — bỏ qua, không trả lời.")
            return

        # ── #donhang (trang 1) ────────────────────────────────────────────────
        if cmd_lower == "#donhang":
            _donhang_live = _DONHANG_DATA

            sub_id = _chuan_hoa_ten(sender_name) if sender_name else ""
            if not sub_id:
                try:
                    await self._send_text_reply(page, " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!", with_mention=False, msg_id=msg_id)
                except Exception as e:
                    log.error(f"❌ Lỗi gửi thông báo #donhang không tên: {e}")
                return

            if sub_id not in _donhang_live:
                reply = " ❌Rất tiếc! Không tìm thấy đơn hàng của bạn😿\n\n✅ Hãy quay lại kiểm tra vào\n👉SÁNG NGÀY MAI👈 khi ad Thư thông báo trên nhóm nếu bạn đặt trước 👉23h59p hôm nay👈 nhé!"
            else:
                reply = " " + _format_donhang_page(_donhang_live, sub_id, 1)

            try:
                await self._send_text_reply(page, reply, sender_name, msg_id=msg_id)
            except Exception as e:
                log.error(f"❌ Lỗi gửi #donhang: {e}")
            return

        # ── #donhangN (trang N >= 2) ──────────────────────────────────────────
        m_donhang_page = re.match(r'^#donhang([1-9]\d*)$', cmd_lower)
        if m_donhang_page:
            page_num = int(m_donhang_page.group(1))
            _donhang_live = _DONHANG_DATA

            sub_id = _chuan_hoa_ten(sender_name) if sender_name else ""
            if not sub_id:
                try:
                    await self._send_text_reply(page, " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!", with_mention=False, msg_id=msg_id)
                except Exception as e:
                    log.error(f"❌ Lỗi gửi thông báo #donhangN không tên: {e}")
                return

            reply = " " + _format_donhang_page(_donhang_live, sub_id, page_num)
            try:
                await self._send_text_reply(page, reply, sender_name, msg_id=msg_id)
            except Exception as e:
                log.error(f"❌ Lỗi gửi #donhang{page_num}: {e}")
            return

        # ── #vitien ───────────────────────────────────────────────────────────
        if cmd_lower == "#vitien":
            _vitien_live = _VITIEN_DATA
            try:
                _da_nhan_live = _load_da_nhan("da_nhan_by_subid.json")
            except Exception:
                _da_nhan_live = _DA_NHAN_DATA

            sub_id = _chuan_hoa_ten(sender_name) if sender_name else ""
            if not sub_id:
                try:
                    await self._send_text_reply(page, " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!", with_mention=False, msg_id=msg_id)
                except Exception as e:
                    log.error(f"❌ Lỗi gửi thông báo #vitien không tên: {e}")
                return

            wallet = _calc_vitien(_vitien_live, _da_nhan_live, sub_id)
            if wallet is None:
                reply = " Rất tiếc! Không tìm thấy thông tin ví của SẾP. Hãy liên hệ trưởng nhóm Thư 🌷"
            else:
                reply = (
                    f"💳 VÍ TIỀN CỦA SẾP!\n\n"
                    f"🔸 Đang chờ xử lý: {_fmt_money(wallet['dang_cho'])}\n"
                    f"🔹 Đã hoàn thành: {_fmt_money(wallet['hoan_thanh_chua_rut'])}\n\n"
                    f"> Tiền sẽ xuống phần có thể rút ngay sau 1 ngày từ ngày đã hoàn thành <\n\n"
                    f"🌷 Có thể rút ngay: {_fmt_money(wallet['co_the_rut_hien'])}\n\n"
                    f"💌 Đã nhận: {_fmt_money(wallet['da_nhan'])}"
                )
            try:
                await self._send_text_reply(page, reply, sender_name, msg_id=msg_id)
            except Exception as e:
                log.error(f"❌ Lỗi gửi #vitien: {e}")
            return

        # ── #My_ID ───────────────────────────────────────────────────────────
        if cmd_lower == "#my_id":
            sub_id = _chuan_hoa_ten(sender_name) if sender_name else ""
            if not sub_id:
                try:
                    await self._send_text_reply(page, " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!", with_mention=False, msg_id=msg_id)
                except Exception as e:
                    log.error(f"❌ Lỗi gửi thông báo #My_ID không tên: {e}")
                return

            reply = f" My ID của bạn là: {sub_id}🌷"

            try:
                await self._send_text_reply(page, reply, sender_name, msg_id=msg_id)
            except Exception as e:
                log.error(f"❌ Lỗi gửi #My_ID: {e}")
            return

        # ── #huongdan ────────────────────────────────────────────────────────
        if cmd_lower == "#huongdan":
            reply = (
                "🌷Hello các thành viên mới, mình sẽ hướng dẫn mọi người cách mua sắm trên Shopee để được hoàn tiền hoa hồng nhé 👇👇\n\n"
                "🔸Bước 1: Gửi link sản phẩm bạn muốn mua vào nhóm.\n\n"
                "🔸Bước 2: Mình sẽ tag bạn và gửi lại link của sản phẩm đó\n\n"
                "🔸Bước 3:\n"
                "✅ Xóa sản phẩm đó khỏi giỏ hàng (nếu có)\n"
                "✅ Bấm link mình gửi có tag bạn rồi thêm giỏ hoặc mua ngay\n"
                "❌ Không xem live hoặc video khi mua vì sẽ ko được hoa hồng\n"
                "❌ Không bấm vào link của người khác hay áp mã giảm giá của người khác sau khi đã bấm link mình gửi\n\n"
                "🔸Bước 4: Các đơn bạn đặt từ 00:00 - 23:59 hôm nay thì sang sáng ngày mai khi mình thông báo có chuyển đổi từ Shopee lên nhóm thì mn nhắn #donhang và #vitien lên nhóm để kiểm tra nhé\n\n"
                "🔸Bước 5: Sau khi bạn ấn đã nhận hàng trên app Shopee tiền sẽ xuống phần đã hoàn thành và sau 1 ngày sẽ xuống phần có thể rút ngay. Cách tính: (hoa hồng - 10%) * 80%. Ví dụ sản phẩm có hoa hồng 100k thì hh bạn nhận\n"
                "= (100k - 10%)*80% = 72.000đ\n\n"
                "🟢 Các câu lệnh bạn sử dụng để nhắn trong nhóm bot sẽ gửi lại tin nhắn tự động 👇\n\n"
                "#donhang 👉 kiểm tra những đơn hàng bạn đã mua\n\n"
                "#vitien 👉 kiểm tra ví tiền của bạn\n\n"
                "#thongtin_tên ngân hàng + stk\n"
                "(VD: thongtin_mbbank066099)\n"
                "👉 lưu stk của bạn để rút tiền\n\n"
                "#thongtin 👉 kiểm tra lại thông tin\n\n"
                "#ruttien_ số tiền\n"
                "(VD: #ruttien_50000) 👉 rút số tiền bạn đang có ở phần có thể rút ngay\n\n"
                "💡Mình đã hoàn thành hướng dẫn bạn, nếu có thắc mắc vui lòng nhắn cho trưởng nhóm Thư để được hỗ trợ. Thank you!"
            )
            try:
                await self._ensure_correct_group()
                await self._scroll_to_bottom()
                await self._click_chat_box(page)
                await asyncio.sleep(0.03)
                await page.evaluate("(t) => navigator.clipboard.writeText(t)", reply)
                await page.keyboard.press("Control+v")
                await asyncio.sleep(0.05)
                await self._ensure_correct_group()
                await self._click_send_button(page)
            except Exception as e:
                log.error(f"❌ Lỗi gửi #huongdan: {e}")
            return

        # ── #ruttien (hướng dẫn, không kèm số) ──────────────────────────────
        if cmd_lower == "#ruttien":
            reply = " Hãy gửi đúng định dạng để rút tiền nhé!\nVí dụ: #ruttien_50000"
            try:
                await self._send_text_reply(page, reply, sender_name, msg_id=msg_id)
            except Exception as e:
                log.error(f"❌ Lỗi gửi #ruttien hướng dẫn: {e}")
            return

        # ── #ruttien_<số> ─────────────────────────────────────────────────────
        m_ruttien = re.match(r'^#ruttien_([\d.,]+)$', cmd_lower)
        if m_ruttien:
            so_tien_str = m_ruttien.group(1).replace(".", "").replace(",", "")
            try:
                so_tien_rut = float(so_tien_str)
            except ValueError:
                try:
                    await self._send_text_reply(page, " Số tiền không hợp lệ. Vui lòng nhập lại nhé 🌷", sender_name, msg_id=msg_id)
                except Exception:
                    pass
                return

            _vitien_r = _VITIEN_DATA
            try:
                _da_nhan_r = _load_da_nhan("da_nhan_by_subid.json")
            except Exception:
                _da_nhan_r = _DA_NHAN_DATA

            sub_id_r = _chuan_hoa_ten(sender_name) if sender_name else ""
            if not sub_id_r:
                try:
                    await self._send_text_reply(page, " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!", with_mention=False, msg_id=msg_id)
                except Exception:
                    pass
                return

            wallet_r = _calc_vitien(_vitien_r, _da_nhan_r, sub_id_r)
            if wallet_r is None:
                try:
                    await self._send_text_reply(page, " Không tìm thấy thông tin ví của bạn 🌷", sender_name, msg_id=msg_id)
                except Exception:
                    pass
                return

            co_the_rut_hien = wallet_r["co_the_rut_hien"]
            if so_tien_rut > co_the_rut_hien:
                reply_err = (
                    f" Số tiền rút vượt quá số dư có thể rút!\n"
                    f"💰 Tối đa SẾP có thể rút: {_fmt_money(co_the_rut_hien)}\n"
                    f"🌷 Vui lòng nhập lại nhé!"
                )
                try:
                    await self._send_text_reply(page, reply_err, sender_name, msg_id=msg_id)
                except Exception:
                    pass
                return

            # Lấy STK
            try:
                _thongtin_r = _load_thongtin(THONGTIN_FILE)
                stk_info = _thongtin_r.get(sub_id_r, {}).get("info", "Chưa lưu STK")
            except Exception:
                stk_info = "Chưa lưu STK"

            # Ghi tăng da_nhan
            entry_r = dict(_da_nhan_r.get(sub_id_r, {}))
            da_nhan_cu = sum(entry_r.values()) if entry_r else 0.0
            from datetime import date as _datetoday
            thang_key = f"t{_datetoday.today().month}"
            entry_r[thang_key] = round(entry_r.get(thang_key, 0.0) + so_tien_rut, 2)
            _da_nhan_r[sub_id_r] = entry_r
            try:
                import json as _json_r
                with open("da_nhan_by_subid.json", "w", encoding="utf-8") as _fw:
                    _json_r.dump(
                        {k: (v if isinstance(v, dict) else {"t0": v}) for k, v in _da_nhan_r.items()},
                        _fw, ensure_ascii=False, indent=2
                    )
                _DA_NHAN_DATA[sub_id_r] = entry_r  # cập nhật RAM ngay, không cần khởi động lại

                # ── Đẩy da_nhan mới nhất thẳng lên Upstash Redis ──────────────────
                # Chạy nền, không block phản hồi cho khách; đợi 2s rồi mới gửi để
                # đảm bảo file da_nhan_by_subid.json đã ghi xong ổn định trên đĩa.
                # [MỚI] Đọc TOÀN BỘ file da_nhan_by_subid.json từ đĩa rồi ghi THẲNG
                # lên Upstash Redis (dùng chung giữa phuongthaovip + hoan-vi-web),
                # không cần gọi HTTP sang từng web nữa.
                async def _push_danhan_bg(sub_id_log: str, so_tien_log: float):
                    try:
                        await asyncio.sleep(2)
                        loop_bg = asyncio.get_running_loop()
                        from bot_data_loader import push_danhan_from_file_to_upstash
                        ok_bg = await loop_bg.run_in_executor(
                            None, push_danhan_from_file_to_upstash, "da_nhan_by_subid.json"
                        )
                        log.info(
                            f"📤 [ruttien] Đồng bộ da_nhan lên Upstash cho [{sub_id_log}] (+{so_tien_log}): {'OK' if ok_bg else 'THẤT BẠI'}"
                        )
                        if not ok_bg:
                            log.error(
                                f"❌ [ruttien] GHI UPSTASH THẤT BẠI cho [{sub_id_log}] "
                                f"— kiểm tra UPSTASH_REDIS_REST_URL / TOKEN / mạng ngay!"
                            )
                    except Exception as e_bg:
                        log.warning(f"⚠️ [ruttien] Đồng bộ da_nhan thất bại: {e_bg}")

                _spawn_background_task(_push_danhan_bg(sub_id_r, so_tien_rut))
            except Exception as e:
                log.error(f"❌ Lỗi ghi da_nhan: {e}")
                try:
                    await self._send_text_reply(page, " Có lỗi xảy ra khi xử lý yêu cầu rút tiền. Vui lòng thử lại 🌷", sender_name, msg_id=msg_id)
                except Exception:
                    pass
                return

            # ── Tăng bộ đếm thứ tự rút tiền (Thư_N) ──────────────────────
            COUNTER_FILE = "ruttien_counter.json"
            rut_order = 1
            try:
                import json as _json_ctr
                try:
                    with open(COUNTER_FILE, "r", encoding="utf-8") as _fc:
                        _ctr_data = _json_ctr.load(_fc)
                except FileNotFoundError:
                    _ctr_data = {"counter": 0}
                _ctr_data["counter"] = int(_ctr_data.get("counter", 0)) + 1
                rut_order = _ctr_data["counter"]
                with open(COUNTER_FILE, "w", encoding="utf-8") as _fc:
                    _json_ctr.dump(_ctr_data, _fc, ensure_ascii=False, indent=2)
                log.info(f"🔎 #ruttien — thứ tự rút: Thư_{rut_order}")
            except Exception as e:
                log.warning(f"⚠️ Không ghi được counter ruttien: {e}")
                rut_order = 1

            reply_ok = (
                f"  Chúc mừng SẾP đã rút tiền thành công 🎉\n"
                f"💰 Số tiền: {_fmt_money(so_tien_rut)}\n"
                f"🏦 STK: {stk_info}\n"
                f"🔎 ID: Thư_{rut_order}\n"
                f"🌷 Trưởng nhóm Thư sẽ chuyển tiền cho bạn trong thời gian sớm nhất có thể!"
            )
            try:
                await self._send_text_reply(page, reply_ok, sender_name, msg_id=msg_id)
            except Exception as e:
                log.error(f"❌ Lỗi gửi xác nhận #ruttien: {e}")
            return

        # ── #thongtin / #thongtin_<bank><số> ─────────────────────────────────
        _thongtin_idx = cmd_lower.find("#thongtin")
        if _thongtin_idx >= 0:
            thongtin_cmd = cmd_lower[_thongtin_idx:].split()[0]
            sub_id_t = _chuan_hoa_ten(sender_name) if sender_name else ""

            if thongtin_cmd.startswith("#thongtin_"):
                info = _parse_thongtin_info(command.strip())
                if not info:
                    reply_text = " Thông tin không hợp lệ. Vui lòng nhập lại đúng định dạng #thongtin_<ngânhàng><số>."
                elif not sub_id_t:
                    reply_text = " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!"
                else:
                    _thongtin_live = _load_thongtin(THONGTIN_FILE)
                    if sub_id_t in _thongtin_live:
                        reply_text = " Bạn đã lưu thông tin trước đó rồi 🌷"
                    else:
                        _thongtin_live[sub_id_t] = {"sub_id": sub_id_t, "info": info}
                        _save_thongtin(_thongtin_live)
                        _THONGTIN_DATA.update(_thongtin_live)
                        log.info(f"💌 #thongtin — đã lưu [{sub_id_t}]: {info}")
                        reply_text = " Bạn đã lưu thông tin thành công 🌷"
            else:
                if not sub_id_t:
                    reply_text = " Hãy gửi lại tin nhắn 1 lần nữa nếu bạn vẫn cần!"
                else:
                    _thongtin_live = _load_thongtin(THONGTIN_FILE)
                    if sub_id_t in _thongtin_live:
                        info_raw = _thongtin_live[sub_id_t].get("info", "")
                        reply_text = " " + _format_thongtin(info_raw, zalo_name=sender_name)
                    else:
                        reply_text = " Tính năng này sẽ sớm ra mắt trong thời gian tới 🌷"

            try:
                await self._send_text_reply(page, reply_text, sender_name, msg_id=msg_id)
                log.info(f"💌 #thongtin — đã gửi reply cho [{sender_name or sub_id_t}]")
            except Exception as e:
                log.error(f"❌ Lỗi xử lý #thongtin: {e}")
            return

        # ── Lệnh không nhận dạng được ─────────────────────────────────────────
        COMMAND_REPLY = " Tính năng này sẽ sớm ra mắt trong thời gian tới 🌷"
        try:
            await self._send_text_reply(page, COMMAND_REPLY, sender_name)
            log.info(f"✅ Đã trả lời lệnh [{command}] cho [{sender_name or '???'}]")
        except Exception as e:
            log.error(f"❌ Lỗi xử lý lệnh [{command}]: {e}")

    # ── Xử lý link Shopee ─────────────────────────────────────────────────────

    async def _handle(self, link: str, sender_name: str, node_id: str = "", msg_id: str = ""):
        """
        [OPT v27] CLICK REPLY NGAY → RỒI MỚI CHỜ CONVERT + COMMISSION SONG SONG.

        Flow mới:
          1. Nhận link  → ngay lập tức scroll + click nút Trả lời (quote banner hiện lên)
          2. TRONG LÚC CHỜ quote banner: chạy convert + commission đồng thời (asyncio.gather)
          3. Khi cả 2 xong → paste nội dung → đóng preview card → gửi

        Tiết kiệm: toàn bộ thời gian hover/click reply (~0.4-0.8s) được ẩn sau thời gian chờ API.
        """
        short_url       = None
        mentioned       = False
        commission_info = None
        try:
            self._processing_links.add(link)

            page = self._zalo_page
            self.link_senders[link] = sender_name

            # ── BƯỚC 1: Click nút Trả lời NGAY (không đợi convert) ──────────────
            await self._ensure_correct_group()
            await self._scroll_to_bottom()

            replied_by_id = False
            if msg_id:
                replied_by_id = await self._reply_to_message_id(page, msg_id)
                log.info(f"{'✅ [EARLY] Reply by msgId' if replied_by_id else '⚠️ [EARLY] msgId fail → thử fallback link'}: {msg_id}")
            if not replied_by_id:
                replied_by_id = await self._reply_to_message_by_link(page, link)
                log.info(f"{'✅ [EARLY] Reply by link bubble' if replied_by_id else '⚠️ [EARLY] Cả 2 chiến lược reply đều thất bại'}")

            # ── BƯỚC 2: TRONG LÚC quote banner đang hiển thị → convert + commission SONG SONG ──
            async def _run_converter():
                # Dùng Zalo ID của người gửi (userId từ data-qid) làm sub_id
                sid = sender_name if sender_name else SUB_ID
                log.info(f"🏷 sub_id = {sid!r} (Zalo ID người gửi)")
                return await process_link(link, sub_id=sid)

            async def _run_commission():
                return await check_commission(link)

            log.info(f"🚀 [PARALLEL] Converter + Commission trong lúc quote banner hiển thị: {link[:60]}")
            ext_result, commission_result = await asyncio.gather(
                _run_converter(),
                _run_commission(),
            )
            short_url       = ext_result
            commission_info = commission_result
            log.info(f"✅ Done — url={short_url[:60]}")

            await self._zalo_page.evaluate(_JS_PURGE_LINK, link)
            self._processing_links.add(short_url)

            # ── BƯỚC 3: Paste nội dung vào ô chat (quote banner vẫn đang giữ) ───
            mentioned = False  # @mention đã bị loại bỏ
            full_message, _ = self._build_message_parts(short_url, sender_name, mentioned or replied_by_id, commission_info)

            await self._click_chat_box(page)
            await asyncio.sleep(0.03)
            await page.evaluate("(t) => navigator.clipboard.writeText(t)", full_message)
            await page.keyboard.press("Control+v")

            # ── BƯỚC 4: Đóng preview card (nếu có) → gửi ───────────────────────
            await asyncio.sleep(0.05)  # [OPT] 0.08s→0.05s
            if replied_by_id:
                closed = await self._click_close_btn(page)
                log.info(f"{'✅ Đã đóng quote banner' if closed else '⚠️ Không tìm thấy quote banner để đóng'}")
            await asyncio.sleep(0.05)  # [OPT] 0.08s→0.05s
            await self._ensure_correct_group()
            await self._click_send_button(page)

            await self._zalo_page.evaluate(_JS_PURGE_LINK, short_url)
            log.info(f"✅ Đã gửi (replied_by_id={replied_by_id}, mention={mentioned}): {short_url}")

        except Exception as e:
            log.error(f"❌ Lỗi xử lý {link}: {e}")
            if short_url is None and sender_name:
                try:
                    page = self._zalo_page
                    await self._ensure_correct_group()
                    await self._scroll_to_bottom()

                    if not replied_by_id:
                        if msg_id:
                            replied_by_id = await self._reply_to_message_id(page, msg_id)
                        if not replied_by_id:
                            replied_by_id = await self._reply_to_message_by_link(page, link)

                    full_message_fb = "🌟 Oh no SẾP ơi!\n\n⚠️ Link bạn gửi không phải link sản phẩm. Vui lòng gửi lại link sản phẩm nhé!"

                    await self._click_chat_box(page)
                    await asyncio.sleep(0.03)
                    await page.evaluate("(t) => navigator.clipboard.writeText(t)", full_message_fb)
                    await page.keyboard.press("Control+v")

                    await asyncio.sleep(0.05)
                    if replied_by_id:
                        closed_fb = await self._click_close_btn(page)
                        log.info(f"{'✅ Đã đóng quote banner (fallback)' if closed_fb else '⚠️ Không tìm thấy quote banner (fallback)'}")
                    await asyncio.sleep(0.05)
                    await self._ensure_correct_group()
                    await self._click_send_button(page)
                    log.info(f"⚠️ Đã gửi thông báo link lỗi cho [{sender_name}]")
                except Exception as fe:
                    log.error(f"❌ Lỗi gửi thông báo lỗi: {fe}")
            try:
                await self._zalo_page.evaluate(_JS_PURGE_LINK, link)
            except Exception:
                pass

        finally:
            self._processing_links.discard(link)
            if short_url:
                async def _delayed_unlock(u):
                    await asyncio.sleep(5)
                    self._processing_links.discard(u)
                asyncio.create_task(_delayed_unlock(short_url))
            log.info(f"🔓 Đã mở khóa link: {link}")

    async def _handle_multi(self, links: list, sender_name: str, node_id: str = "", msg_id: str = ""):
        """
        Xử lý TẤT CẢ link từ CÙNG 1 TIN NHẮN:
          - Convert + check hoa hồng TẤT CẢ link song song (asyncio.gather)
          - Reply 1 lần duy nhất vào tin nhắn gốc
          - Chia 5 link / tin nhắn, gửi nhiều tin nếu cần
            Ví dụ: 10 link → 2 tin, 13 link → 3 tin, 3 link → 1 tin
          - Mỗi link có STT liên tục (1. 2. 3. ... 10. 11. ...) xuyên suốt
        """
        LINKS_PER_MSG = 5

        for lnk in links:
            self._processing_links.add(lnk)

        page = self._zalo_page
        short_urls       = [None] * len(links)
        commission_infos = [None] * len(links)
        replied_by_id    = False

        try:
            # ── BƯỚC 1: Convert + Commission TẤT CẢ song song (không click reply sớm) ──
            await self._ensure_correct_group()
            await self._scroll_to_bottom()

            # ── BƯỚC 2: Convert + Commission TẤT CẢ song song ──────────────────
            # Dùng Zalo ID của người gửi (userId từ data-qid) làm sub_id
            sid = sender_name if sender_name else SUB_ID

            async def _do_one(idx: int, lnk: str):
                try:
                    aff_url = await process_link(lnk, sub_id=sid)
                except Exception as e:
                    log.warning(f"⚠️ convert link {idx+1} lỗi: {e} → fallback")
                    aff_url = make_affiliate_link_fallback(lnk)
                try:
                    comm = await check_commission(lnk)
                except Exception:
                    comm = _NO_COMMISSION
                return aff_url, comm

            log.info(f"🚀 [MULTI] Converter + Commission song song cho {len(links)} link")
            results = await asyncio.gather(*[_do_one(i, lnk) for i, lnk in enumerate(links)])
            for i, (aff_url, comm) in enumerate(results):
                short_urls[i]       = aff_url
                commission_infos[i] = comm
                self._processing_links.add(aff_url)
                await self._zalo_page.evaluate(_JS_PURGE_LINK, links[i])

            # ── Helper: rút gọn tên sản phẩm ────────────────────────────────────
            def _shorten_product(name: str, limit: float = 25.0) -> str:
                total = 0.0; cut = 0
                for ci, ch in enumerate(name):
                    total += 1.3 if ch.isupper() else 1.0
                    if total > limit:
                        break
                    cut = ci + 1
                return name[:cut].rstrip() + "..."

            def _weighted_len(s: str) -> float:
                return sum(1.3 if c.isupper() else 1.0 for c in s)

            # ── BƯỚC 3: Build từng block nội dung (STT liên tục toàn batch) ─────
            blocks = []
            for i, (aff_url, comm) in enumerate(zip(short_urls, commission_infos)):
                stt = i + 1
                if comm is _NO_COMMISSION or comm is None:
                    block = (
                        f"{stt}.\n"
                        f"👉 {aff_url}"
                    )
                elif isinstance(comm, dict):
                    product_name = comm.get("productName", "")
                    comm_str     = comm.get("commission_str", "—")
                    comm_pct     = comm.get("commission_pct", "—")
                    if _weighted_len(product_name) > 25.0:
                        product_name = _shorten_product(product_name)
                    p_line = f'"{product_name}"📚\n' if product_name else ""
                    block = (
                        f"{stt}. {p_line}"
                        f"👉 {aff_url}\n"
                        f"🌷 Hoa hồng: {comm_pct} ~ {comm_str}"
                    )
                else:
                    block = (
                        f"{stt}.\n"
                        f"👉 {aff_url}"
                    )
                blocks.append(block)

            FOOTER = (
                "\n\n⚠️ LƯU Ý QUAN TRỌNG:\n"
                "1. Xóa sp này khỏi giỏ hàng nếu có\n"
                "2. Ko xem live trước/sau khi bấm link\n"
                "\n"
                "Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )
            HEADER = "🌟SẾP ơi em gửi!\n"
            SEP = "\n" + "─" * 16 + "\n"

            # ── BƯỚC 4: Chia block thành từng nhóm 5, gửi từng tin nhắn ─────────
            chunks = [blocks[s:s + LINKS_PER_MSG] for s in range(0, len(blocks), LINKS_PER_MSG)]
            total_msgs = len(chunks)
            log.info(f"📨 [MULTI] {len(links)} link → {total_msgs} tin nhắn (5/tin)")

            for msg_idx, chunk in enumerate(chunks):
                # Header chỉ ở tin đầu, footer chỉ ở tin cuối
                header = HEADER if msg_idx == 0 else ""
                footer = FOOTER if msg_idx == total_msgs - 1 else ""
                full_message = header + SEP.join(chunk) + footer

                # Reply vào tin nhắn gốc cho MỌI tin nhắn trong batch
                replied_this = False
                if msg_id:
                    replied_this = await self._reply_to_message_id(page, msg_id)
                if not replied_this:
                    replied_this = await self._reply_to_message_by_link(page, links[0])
                log.info(f"{'✅' if replied_this else '⚠️'} [MULTI] tin {msg_idx+1}/{total_msgs} reply={'ok' if replied_this else 'fail'}")

                await self._click_chat_box(page)
                await asyncio.sleep(0.03)
                await page.evaluate("(t) => navigator.clipboard.writeText(t)", full_message)
                await page.keyboard.press("Control+v")

                await asyncio.sleep(0.08)
                if replied_this:
                    closed = await self._click_close_btn(page)
                    log.info(f"{'✅ Đóng quote banner' if closed else '⚠️ Không tìm thấy quote banner'} (tin {msg_idx+1})")
                await asyncio.sleep(0.08)
                await self._ensure_correct_group()
                await self._click_send_button(page)

                log.info(f"✅ [MULTI] Đã gửi tin {msg_idx+1}/{total_msgs} ({len(chunk)} link)")
                if msg_idx < total_msgs - 1:
                    await asyncio.sleep(0.5)

            for aff_url in short_urls:
                if aff_url:
                    await self._zalo_page.evaluate(_JS_PURGE_LINK, aff_url)
            log.info(f"✅ [MULTI] Hoàn tất {len(links)} link trong {total_msgs} tin nhắn (replied={replied_by_id})")

        except Exception as e:
            log.error(f"❌ [MULTI] Lỗi xử lý {len(links)} link: {e}")
            try:
                await self._zalo_page.evaluate(_JS_PURGE_LINK, links[0])
            except Exception:
                pass

        finally:
            for lnk in links:
                self._processing_links.discard(lnk)
            for aff_url in short_urls:
                if aff_url:
                    async def _delayed_unlock(u):
                        await asyncio.sleep(5)
                        self._processing_links.discard(u)
                    asyncio.create_task(_delayed_unlock(aff_url))

    def _build_message(
        self,
        short_url: str,
        sender_name: str,
        mentioned: bool,
        commission_info=None,
    ) -> str:
        if commission_info is _NO_COMMISSION or commission_info is None:
            body = (
                f"Ới mua qua link này được hoàn tiền hoa hồng nhé 👇\n"
                f"\n"
                f"1.\n"
                f"👉 {short_url}\n"
                f"\n"
                f"⚠️LƯU Ý QUAN TRỌNG:\n"
                f"1. Xóa sp này khỏi giỏ hàng nếu có\n"
                f"2. Ko xem live trước/sau khi bấm link\n"
                f"\n"
                f"Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )
        elif isinstance(commission_info, dict):
            product_name = commission_info.get("productName", "")
            comm_str     = commission_info.get("commission_str", "—")
            comm_pct     = commission_info.get("commission_pct", "—")
            MAX_NAME_LEN = 25.0
            def _weighted_len(s):
                return sum(1.3 if c.isupper() else 1.0 for c in s)
            if _weighted_len(product_name) > MAX_NAME_LEN:
                total = 0.0
                cut = 0
                for i, c in enumerate(product_name):
                    total += 1.3 if c.isupper() else 1.0
                    if total > MAX_NAME_LEN:
                        break
                    cut = i + 1
                product_name = product_name[:cut].rstrip() + "..."
            product_line = f'"{product_name}"📚\n' if product_name else ""
            body = (
                f"Ới mua qua link này được hoàn tiền hoa hồng nhé 👇\n"
                f"\n"
                f"1. {product_line}"
                f"👉 {short_url}\n"
                f"🌷 Hoa hồng: {comm_pct} ~ {comm_str}\n"
                f"\n"
                f"⚠️LƯU Ý QUAN TRỌNG:\n"
                f"1. Xóa sp này khỏi giỏ hàng nếu có\n"
                f"2. Ko xem live trước/sau khi bấm link\n"
                f"\n"
                f"Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )
        else:
            body = (
                f"Ới mua qua link này được hoàn tiền hoa hồng nhé 👇\n"
                f"\n"
                f"1.\n"
                f"👉 {short_url}\n"
                f"\n"
                f"⚠️LƯU Ý QUAN TRỌNG:\n"
                f"1. Xóa sp này khỏi giỏ hàng nếu có\n"
                f"2. Ko xem live trước/sau khi bấm link\n"
                f"\n"
                f"Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )

        return f"{body}"

    def _build_message_parts(
        self,
        short_url: str,
        sender_name: str,
        mentioned: bool,
        commission_info=None,
    ) -> tuple:
        """
        Trả về (full_message, None) — link affiliate được GỘP CHUNG vào tin nhắn.
        Chỉ gửi 1 tin duy nhất, Zalo sẽ render preview card từ link trong nội dung.
        """
        if commission_info is _NO_COMMISSION or commission_info is None:
            full_message = (
                "Ới mua qua link này được hoàn tiền hoa hồng nhé 👇\n"
                "\n"
                "1.\n"
                f"👉 {short_url}\n"
                "\n"
                "⚠️LƯU Ý QUAN TRỌNG:\n"
                "1. Xóa sp này khỏi giỏ hàng nếu có\n"
                "2. Ko xem live trước/sau khi bấm link\n"
                "\n"
                "Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )
        elif isinstance(commission_info, dict):
            product_name = commission_info.get("productName", "")
            comm_str     = commission_info.get("commission_str", "—")
            comm_pct     = commission_info.get("commission_pct", "—")
            MAX_NAME_LEN = 25.0
            def _weighted_len(s):
                return sum(1.3 if c.isupper() else 1.0 for c in s)
            if _weighted_len(product_name) > MAX_NAME_LEN:
                total = 0.0
                cut = 0
                for i, c in enumerate(product_name):
                    total += 1.3 if c.isupper() else 1.0
                    if total > MAX_NAME_LEN:
                        break
                    cut = i + 1
                product_name = product_name[:cut].rstrip() + "..."
            product_line = f'"{product_name}"📚\n' if product_name else ""
            full_message = (
                "Ới mua qua link này được hoàn tiền hoa hồng nhé 👇\n"
                "\n"
                f"1. {product_line}"
                f"👉 {short_url}\n"
                f"🌷 Hoa hồng: {comm_pct} ~ {comm_str}\n"
                "\n"
                "⚠️LƯU Ý QUAN TRỌNG:\n"
                "1. Xóa sp này khỏi giỏ hàng nếu có\n"
                "2. Ko xem live trước/sau khi bấm link\n"
                "\n"
                "Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )
        else:
            full_message = (
                "Ới mua qua link này được hoàn tiền hoa hồng nhé 👇\n"
                "\n"
                "1.\n"
                f"👉 {short_url}\n"
                "\n"
                "⚠️LƯU Ý QUAN TRỌNG:\n"
                "1. Xóa sp này khỏi giỏ hàng nếu có\n"
                "2. Ko xem live trước/sau khi bấm link\n"
                "\n"
                "Web chuyển link + check đơn: https://hoantien-dautay.vercel.app 📌"
            )
        return full_message, None  # link_part = None vì đã gộp vào full_message

    # ── Zalo page utilities    # ── Zalo page utilities ───────────────────────────────────────────────────

    async def _scroll_to_bottom(self):
        try:
            await self._zalo_page.evaluate("""
                () => {
                    const sels = [
                        '[class*="message-view__scroll"]','[class*="message-view"]',
                        '[class*="threadChat"]','[class*="chat-content"]','[class*="chatContent"]',
                    ];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) { el.scrollTop = el.scrollHeight; return true; }
                    }
                    return false;
                }
            """)
        except Exception:
            pass

    async def _ensure_correct_group(self):
        await self._scroll_to_bottom()
        if not self._pinned_group_title and not self._pinned_group_id:
            return True
        try:
            import unicodedata
            def norm(s): return unicodedata.normalize("NFC", s or "").strip().lower()

            if self._pinned_group_id:
                current_id = await self._get_current_group_id()
                if current_id and current_id == self._pinned_group_id:
                    return True
                if current_id and current_id != self._pinned_group_id:
                    try:
                        await self._click_chat_box(self._zalo_page)
                        await self._zalo_page.keyboard.press("Control+a")
                        await self._zalo_page.keyboard.press("Delete")
                    except Exception:
                        pass
                    raise RuntimeError(f"SAI NHÓM ID: hiện={current_id}, cần={self._pinned_group_id}")

            current_title = await self._zalo_page.evaluate("""
                () => {
                    const sels = [
                        '[class*="conv-name"]','[class*="convName"]',
                        '[class*="group-name"]','[class*="groupName"]',
                        '[class*="chat-header"] [class*="title"]',
                        'h1,h2,h3',
                    ];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) {
                            const t = (el.innerText || el.textContent || '').trim();
                            if (t && t.length > 0 && t.length < 100) return t;
                        }
                    }
                    return '';
                }
            """)
            if not current_title or norm(self._pinned_group_title) in norm(current_title):
                return True

            try:
                await self._click_chat_box(self._zalo_page)
                await self._zalo_page.keyboard.press("Control+a")
                await self._zalo_page.keyboard.press("Delete")
            except Exception:
                pass
            raise RuntimeError(f"SAI NHÓM: đang ở '{current_title}', cần '{self._pinned_group_title}'")

        except RuntimeError:
            raise
        except Exception:
            pass
        return True

    _JS_GET_CHAT_BOX_RECT = """
        () => {
            const all = [...document.querySelectorAll("div[contenteditable='true']")];
            let best = null, bestBottom = -1;
            for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (r.top < 100) continue;
                if (r.bottom > bestBottom) { bestBottom = r.bottom; best = el; }
            }
            if (!best) return null;
            const r = best.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
    """

    async def _click_chat_box(self, page) -> bool:
        try:
            rect = await page.evaluate(self._JS_GET_CHAT_BOX_RECT)
            if rect and rect.get("x") and rect.get("y"):
                await page.mouse.click(rect["x"], rect["y"])
                await asyncio.sleep(0.03)  # [OPT] 0.05s→0.03s
                return True
            boxes = page.locator("div[contenteditable='true']")
            count = await boxes.count()
            for i in range(count - 1, -1, -1):
                el = boxes.nth(i)
                try:
                    box_rect = await el.bounding_box()
                    if box_rect and box_rect.get("y", 0) > 100:
                        await el.click(timeout=2000)
                        await asyncio.sleep(0.03)  # [OPT] 0.05s→0.03s
                        return True
                except Exception:
                    continue
            return False
        except Exception as e:
            log.warning(f"⚠️ _click_chat_box lỗi: {e}")
            return False

    async def _send_by_enter(self, page):
        """Chỉ dùng cho tin text thuần (không có link) — không bao giờ có preview."""
        await self._click_chat_box(page)
        await asyncio.sleep(0.05)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.1)

    async def _click_close_btn(self, page) -> bool:
        """Click nút X đóng QUOTE BANNER bằng cách tìm banner → tính tọa độ góc phải → click."""
        await asyncio.sleep(0.05)  # [OPT] 0.1s→0.05s

        # Tìm banner quote/reply, lấy tọa độ → click vào góc phải (nơi nút X nằm)
        result = await page.evaluate("""
            () => {
                // Tìm banner reply/quote trong khu vực compose
                const bannerSels = [
                    '[class*="reply-banner"]',
                    '[class*="replyBanner"]',
                    '[class*="quote-banner"]',
                    '[class*="quoteBanner"]',
                    '[class*="compose-banner"]',
                    '[class*="composeBanner"]',
                    '[class*="reply-preview"]',
                    '[class*="replyPreview"]',
                ];
                // Ưu tiên: dùng selector chính xác từ DOM
                const exactSelectors = [
                    '.quote-close',
                    'i.fa-Close_24_Line.quote-close',
                    '[data-translate-key="STR_CLOSE"]',
                    '.quote-banner .quote-close',
                    '[class*="quote-banner"] [class*="quote-close"]',
                ];
                for (const qs of exactSelectors) {
                    const el = document.querySelector(qs);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top >= 0 && r.top <= window.innerHeight) {
                        el.click();
                        return { x: r.left + r.width / 2, y: r.top + r.height / 2, found: 'exact:' + qs };
                    }
                }

                for (const s of bannerSels) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    if (r.top < 0 || r.bottom < 0 || r.top > window.innerHeight) continue;
                    // Fallback: click góc phải banner
                    return { x: r.right - 15, y: r.top + r.height / 2, found: s };
                }

                // Fallback: tìm bất kỳ element nào chứa text "Trả lời" hoặc "Reply" ở khu vực dưới
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    const r = el.getBoundingClientRect();
                    if (r.top < 450 || r.height > 80 || r.height < 10) continue;
                    if (r.width < 100) continue;
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (txt.startsWith('Trả lời') || txt.startsWith('Reply')) {
                        return { x: r.right - 15, y: r.top + r.height / 2, found: 'text:' + txt.substring(0,20) };
                    }
                }
                return null;
            }
        """)

        if result:
            log.info(f"🔍 Banner found via: {result.get('found')} → click ({result['x']:.0f}, {result['y']:.0f})")
            await page.mouse.click(result['x'], result['y'])
            await asyncio.sleep(0.05)  # [OPT] 0.1s→0.05s
            return True

        log.warning("⚠️ Không tìm thấy quote banner để đóng")
        return False

    async def _click_close_preview_btn(self, page) -> bool:
        """Click nút X đóng PREVIEW CARD (card sản phẩm trong ô soạn tin).
        Dùng selector rộng hơn vì preview card không có class cố định.
        """
        return await page.evaluate("""
            () => {
                const sels = [
                    'i[class*="Close_24_Line"]',
                    '[class*="preview"] [class*="close"]',
                    '[class*="Preview"] [class*="close"]',
                    '[class*="preview"] button',
                    '[class*="linkPreview"] [class*="close"]',
                    '[class*="attach"] [class*="close"]',
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const target = el.closest('button,[role="button"]') || el;
                    target.click();
                    return true;
                }
                return false;
            }
        """)

    async def _has_preview_card(self, page) -> bool:
        """Kiểm tra có card preview sản phẩm đang hiện trong input area không."""
        return await page.evaluate("""
            () => {
                const sels = [
                    '[class*="preview"]','[class*="Preview"]',
                    '[class*="attach"]','[class*="Attach"]',
                    '[class*="link-preview"]','[class*="linkPreview"]',
                    '[class*="compose"]','[class*="Compose"]',
                ];
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                }
                return false;
            }
        """)

    async def _wait_and_close_preview(self, page):
        """Chờ card preview sản phẩm load xong → bấm X → gửi.
        Nếu không tìm được nút X sau nhiều lần thử → dùng Escape để thoát preview.
        """
        LOADING_JS = """
            () => {
                const kw = ['đang lấy thông tin', 'đang lấy'];
                for (const el of document.querySelectorAll('*')) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    for (const node of el.childNodes) {
                        if (node.nodeType !== Node.TEXT_NODE) continue;
                        const txt = node.textContent.toLowerCase().trim();
                        if (kw.some(k => txt.includes(k))) return true;
                    }
                }
                return false;
            }
        """
        # Chờ loading xuất hiện (tối đa 2s) [OPT] 5s→2s — thường xuất hiện ngay
        for _ in range(20):
            if await page.evaluate(LOADING_JS):
                break
            await asyncio.sleep(0.1)

        # Chờ loading biến mất (tối đa 6s) [OPT] 10s→6s
        for _ in range(60):
            if not await page.evaluate(LOADING_JS):
                break
            await asyncio.sleep(0.1)

        await asyncio.sleep(0.15)  # [OPT] 0.2→0.15s

        # Thử click nút X preview tối đa 3 lần (dùng hàm riêng, không nhầm với quote banner)
        closed = False
        for attempt in range(3):
            closed = await self._click_close_preview_btn(page)
            if closed:
                log.info(f"✅ Đã đóng preview (lần {attempt+1})")
                break
            await asyncio.sleep(0.2)

        # Fallback: nếu vẫn không đóng được, kiểm tra preview còn không
        if not closed:
            still_open = await self._has_preview_card(page)
            if still_open:
                log.warning("⚠️ Không click được nút X preview — thử Escape để dismiss")
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
                # Kiểm tra lại, nếu Escape làm mất focus chatbox thì click lại
                still_open2 = await self._has_preview_card(page)
                if still_open2:
                    log.warning("⚠️ Preview vẫn còn sau Escape — tiếp tục gửi bằng Enter")
            else:
                log.info("✅ Preview đã tự đóng (không cần click X)")

        await asyncio.sleep(0.2)

    async def _click_send_button(self, page):
        """Click nút Gửi màu xanh — dùng mouse click tọa độ góc phải dưới viewport."""
        vp = await page.evaluate("() => ({ w: window.innerWidth, h: window.innerHeight })")
        x = vp['w'] - 28
        y = vp['h'] - 28
        await page.mouse.click(x, y)
        await asyncio.sleep(0.1)
        log.info(f"✅ Click nút Gửi tọa độ ({x}, {y})")

    async def stop(self):
        global _commission_session
        if _commission_session and not _commission_session.closed:
            await _commission_session.close()
            _commission_session = None
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()


# ==================== CHẠY ====================


async def _poll_reload_loop():
    """Poll Vercel mỗi 10 giây xem có lệnh reload không.
    Flow an toàn:
      1. Phát hiện cờ pending → load vào biến TẠM (khách vẫn dùng data cũ)
      2. Kiểm tra data hợp lệ
      3. Atomic swap → ghi kết quả SUCCESS lên Redis → reset cờ
      Nếu lỗi bất kỳ bước nào → ghi kết quả FAILED lên Redis, GIỮ data cũ, KHÔNG reset cờ
      → web hiển thị đúng trạng thái thành công / thất bại
    """
    import urllib.request as _ur
    import json as _json
    global _DONHANG_DATA, _VITIEN_DATA
    from bot_data_loader import load_donhang_remote, load_vitien_remote, VERCEL_BASE_URL, _fetch_json, _CACHE
    log.info("🔁 Bắt đầu poll lệnh reload từ Vercel (mỗi 10 giây)...")

    def _strict_load_donhang() -> dict:
        """Fetch trực tiếp /api/data/donhang — raise nếu lỗi (không fallback êm ái
        như load_donhang_remote), để cơ chế retry/báo lỗi bên dưới hoạt động đúng."""
        data = _fetch_json(f"{VERCEL_BASE_URL}/api/data/donhang")
        _CACHE["donhang"] = data
        return data

    def _strict_load_vitien() -> dict:
        data = _fetch_json(f"{VERCEL_BASE_URL}/api/data/vitien")
        _CACHE["vitien"] = data
        return data

    def _post_status(status_obj: dict):
        """Ghi kết quả reload lên Redis để web hiển thị."""
        try:
            body = _json.dumps({"poll_result": status_obj}).encode("utf-8")
            req2 = _ur.Request(
                f"{VERCEL_BASE_URL}/api/reload",
                data=body,
                headers={"User-Agent": "ZaloBot/1.0", "Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(req2, timeout=8)
        except Exception as e2:
            log.warning(f"⚠️ [poll_reload] Không ghi được status lên Vercel: {e2}")

    def _reset_flag():
        """Reset cờ reload_flag về pending=false sau khi load thành công.
        Gọi 2 request lên cùng /api/reload:
          1. Ghi poll_result=done vào reload_status
          2. Set pending=false vào reload_flag  ← [FIX] dùng /api/reload thay vì /api/reload/reset
        """
        try:
            # Bước 1: ghi status done
            body = _json.dumps({"poll_result": {"state": "done", "done_at": __import__("datetime").datetime.utcnow().isoformat()}}).encode("utf-8")
            req3 = _ur.Request(
                f"{VERCEL_BASE_URL}/api/reload",
                data=body,
                headers={"User-Agent": "ZaloBot/1.0", "Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(req3, timeout=8)
            # Bước 2: reset cờ pending=False — POST /api/reload với {pending: false}
            body2 = _json.dumps({"pending": False}).encode("utf-8")
            req4 = _ur.Request(
                f"{VERCEL_BASE_URL}/api/reload",
                data=body2,
                headers={"User-Agent": "ZaloBot/1.0", "Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(req4, timeout=8)
        except Exception:
            pass

    _poll_count = 0
    while True:
        await asyncio.sleep(10)
        _poll_count += 1
        try:
            req = _ur.Request(
                f"{VERCEL_BASE_URL}/api/reload",
                headers={"User-Agent": "ZaloBot/1.0", "Accept": "application/json"},
            )
            with _ur.urlopen(req, timeout=8) as resp:
                raw_bytes = resp.read()
                raw_str = raw_bytes.decode("utf-8")
                result = _json.loads(raw_str)
            log.info(f"🔍 [poll_reload #{_poll_count}] raw={raw_str!r} parsed={result}")
        except Exception as e:
            log.warning(f"⚠️ [poll_reload #{_poll_count}] Không kết nối được Vercel: {e} — thử lại sau 10 giây")
            continue

        if not result.get("reload"):
            continue

        # ── Có lệnh reload → bắt đầu tải (retry tối đa 3 lần) ───────────────
        log.info("📥 Nhận lệnh reload — đang tải dữ liệu mới vào bộ nhớ tạm...")
        import datetime as _dt

        MAX_ATTEMPTS = 3
        success = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Báo web biết đang loading (kèm số lần thử)
            _post_status({
                "state": "loading",
                "attempt": attempt,
                "max_attempts": MAX_ATTEMPTS,
                "started_at": _dt.datetime.utcnow().isoformat(),
            })
            log.info(f"🔄 [poll_reload] Lần thử {attempt}/{MAX_ATTEMPTS}...")

            # ── Load vào biến TẠM trong executor (không block event loop) ─────
            loop = asyncio.get_running_loop()
            try:
                new_donhang, new_vitien = await asyncio.gather(
                    loop.run_in_executor(None, _strict_load_donhang),
                    loop.run_in_executor(None, _strict_load_vitien),
                )
                # _fetch_json (bên trong bot_data_loader) không bọc try/except nên
                # tự raise khi fetch Vercel lỗi → tới được đây là data thật từ Vercel
                success = True
                break
            except Exception as load_err:
                msg = str(load_err)
                log.warning(f"⚠️ [poll_reload] Lần {attempt}/{MAX_ATTEMPTS} thất bại: {msg}")
                if attempt < MAX_ATTEMPTS:
                    _post_status({
                        "state": "retrying",
                        "attempt": attempt,
                        "max_attempts": MAX_ATTEMPTS,
                        "message": f"Lần {attempt} thất bại, đang thử lại...",
                    })
                    await asyncio.sleep(3)  # chờ 3s trước khi thử lại

        if not success:
            final_msg = f"Thất bại sau {MAX_ATTEMPTS} lần thử — kiểm tra kết nối Vercel/Redis"
            log.error(f"❌ [poll_reload] {final_msg} — GIỮ NGUYÊN dữ liệu cũ, RESET cờ để user thử lại")
            _post_status({"state": "error", "message": final_msg})
            # Reset cờ để user có thể bấm lại
            try:
                body_reset = _json.dumps({"poll_result": {"state": "error", "message": final_msg}}).encode("utf-8")
                req_reset = _ur.Request(
                    f"{VERCEL_BASE_URL}/api/reload",
                    data=body_reset,
                    headers={"User-Agent": "ZaloBot/1.0", "Content-Type": "application/json"},
                    method="POST",
                )
                _ur.urlopen(req_reset, timeout=8)
            except Exception:
                pass
            continue

        # ── Atomic swap (in-place update để mọi reference thấy data mới) ──────
        _DONHANG_DATA.clear()
        _DONHANG_DATA.update(new_donhang)
        _VITIEN_DATA.clear()
        _VITIEN_DATA.update(new_vitien)
        log.info(f"✅ Swap hoàn tất: {len(_DONHANG_DATA)} đơn hàng, {len(_VITIEN_DATA)} ví tiền")

        # ── Ghi kết quả SUCCESS + reset cờ ───────────────────────────────────
        success_status = {
            "state": "success",
            "done_at": _dt.datetime.utcnow().isoformat(),
            "donhang_count": len(_DONHANG_DATA),
            "vitien_count": len(_VITIEN_DATA),
        }
        _post_status(success_status)
        _reset_flag()  # ← FIX: gọi reset cờ pending=False để web không trigger reload lại

        log.info(f"✅ [poll_reload] Hoàn tất sau {attempt} lần thử — dữ liệu mới có hiệu lực ngay!")

async def main():
    global _DONHANG_DATA, _VITIEN_DATA, _DA_NHAN_DATA
    from bot_data_loader import load_donhang_remote, load_vitien_remote
    _DONHANG_DATA = load_donhang_remote()
    log.info(f"📦 Đã load {len(_DONHANG_DATA)} Sub ID từ Vercel (donhang)")
    _VITIEN_DATA = load_vitien_remote()
    log.info(f"💳 Đã load {len(_VITIEN_DATA)} Sub ID từ Vercel (vitien)")
    _THONGTIN_DATA.update(_load_thongtin("thongtin_by_subid.json"))
    log.info(f"💌 Đã load {len(_THONGTIN_DATA)} Sub ID từ thongtin_by_subid.json")
    _DA_NHAN_DATA.update(_load_da_nhan("da_nhan_by_subid.json"))
    log.info(f"💰 Đã load {len(_DA_NHAN_DATA)} Sub ID từ da_nhan_by_subid.json")

    # Khởi động poll loop và bot loop SONG SONG thật sự bằng asyncio.gather
    async def _bot_loop():
        while True:
            bot = ZaloAffiliateBot()
            try:
                await bot.start()
            except KeyboardInterrupt:
                log.info("Dừng bot.")
                return
            except Exception as e:
                log.error(f"💥 Bot crash: {e} — khởi động lại sau 15s...")
                await asyncio.sleep(15)
            finally:
                try:
                    await bot.stop()
                except Exception:
                    pass

    await asyncio.gather(
        _bot_loop(),
        _poll_reload_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("👋 Đã dừng.")
