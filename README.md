# 🤖 Zalo Bot — Hệ thống cập nhật dữ liệu từ xa

Web app upload dữ liệu JSON lên Vercel, bot đọc trực tiếp khi cần.

---

## 📁 Cấu trúc thư mục

```
zalo-data-manager/        ← Deploy lên Vercel
  pages/
    index.js              ← Giao diện upload (mobile-friendly)
    api/
      upload.js           ← POST: upload JSON lên KV store
      data/[type].js      ← GET: bot lấy dữ liệu
      status.js           ← GET: kiểm tra thời gian cập nhật

bot_data_loader.py        ← Đặt cùng thư mục với bot_v23.py
```

---

## 🚀 Bước 1: Deploy lên Vercel

### 1.1 Tạo KV Store (Upstash Redis)

1. Vào [upstash.com](https://upstash.com) → Tạo tài khoản miễn phí
2. Tạo Redis database → Chọn region `ap-southeast-1` (Singapore, gần nhất)
3. Lấy 2 giá trị:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`

### 1.2 Deploy Next.js lên Vercel

1. Đẩy thư mục `zalo-data-manager` lên GitHub
2. Vào [vercel.com](https://vercel.com) → Import project từ GitHub
3. Thêm Environment Variables:

```
UPLOAD_PASSWORD     = Thucute9999
UPSTASH_REDIS_REST_URL    = https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN  = xxxxxxxxxx
```

4. Deploy → Lấy URL dạng `https://thudautay.vercel.app`

---

## 🔧 Bước 2: Tích hợp vào bot

### 2.1 Đặt file

Sao chép `bot_data_loader.py` vào cùng thư mục với `bot_v23.py`.

### 2.2 Sửa bot_data_loader.py

Mở file, thay dòng:
```python
VERCEL_BASE_URL = "https://YOUR-APP.vercel.app"
```
Thành URL thật của bạn:
```python
VERCEL_BASE_URL = "https://thudautay.vercel.app"
```

### 2.3 Sửa bot_v23.py

**Tìm hàm `main()` ở cuối file, thay:**
```python
_DONHANG_DATA = _load_donhang("donhang_by_subid.json")
_VITIEN_DATA = _load_vitien("vitien_by_subid.json")
```

**Thành:**
```python
from bot_data_loader import load_donhang_remote, load_vitien_remote
_DONHANG_DATA = load_donhang_remote()
_VITIEN_DATA  = load_vitien_remote()
```

**Tìm hàm `_handle_command` (xử lý #donhang, #vitien), thêm ở đầu hàm:**
```python
async def _handle_command(self, ...):
    global _DONHANG_DATA, _VITIEN_DATA
    from bot_data_loader import load_donhang_remote, load_vitien_remote
    _DONHANG_DATA = load_donhang_remote()   # ← Thêm dòng này
    _VITIEN_DATA  = load_vitien_remote()    # ← Thêm dòng này
    # ... phần còn lại giữ nguyên
```

---

## 📱 Bước 3: Sử dụng hàng ngày

1. Mở `https://zalo-data-manager.vercel.app` trên điện thoại
2. Nhập mật khẩu đã cài trong `UPLOAD_PASSWORD`
3. Bấm **Chọn file JSON** → chọn file từ bộ nhớ
4. Bấm **Upload** → xong!

Bot sẽ tự động đọc dữ liệu mới nhất mỗi lần khách nhắn `#donhang` hoặc `#vitien`.

---

## 🔐 Bảo mật

- Trang upload yêu cầu mật khẩu → chỉ người biết mật khẩu mới upload được
- API `/api/data/donhang` và `/api/data/vitien` **không cần mật khẩu** → bot đọc tự do
- Nếu muốn bảo vệ API data, thêm header `X-Bot-Secret` vào cả API và bot

---

## ❓ Câu hỏi thường gặp

**Q: Upstash Redis miễn phí được không?**
A: Được! Free tier 10.000 lệnh/ngày, đủ dùng cho bot.

**Q: Bot có cần restart khi upload file mới không?**
A: Không cần. Bot fetch dữ liệu mới mỗi lần nhận lệnh.

**Q: Nếu Vercel/internet lỗi, bot có tiếp tục hoạt động không?**
A: Có. `bot_data_loader.py` có fallback đọc file local nếu không kết nối được.
