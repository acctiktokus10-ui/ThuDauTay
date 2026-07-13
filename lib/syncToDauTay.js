// lib/syncToDauTay.js
//
// Y hệt lib/syncToHoanVi.js bên phuongthaovip-main: mỗi khi upload
// donhang/vitien mới (pages/api/upload.js), hoặc bot ghi da_nhan sau khi
// rút tiền (pages/api/data/[type].js), đẩy dữ liệu sang web hiển thị
// hoantien-dautay-main qua POST /api/sync-data — để web đó luôn thấy
// đúng dữ liệu mới nhất mà KHÔNG cần 2 project dùng chung 1 Redis.
const DAUTAY_SYNC_URL = process.env.DAUTAY_SYNC_URL || ''
const SYNC_SECRET = process.env.SYNC_SECRET || ''

export async function syncToDauTay(type, data) {
  if (!DAUTAY_SYNC_URL) {
    console.warn('[syncToDauTay] Chưa cấu hình DAUTAY_SYNC_URL — bỏ qua đồng bộ')
    return false
  }
  try {
    const res = await fetch(DAUTAY_SYNC_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(SYNC_SECRET ? { 'X-Sync-Secret': SYNC_SECRET } : {}),
      },
      body: JSON.stringify({ type, data }),
      signal: AbortSignal.timeout(25000),
    })
    if (!res.ok) {
      const errText = await res.text().catch(() => '')
      console.warn(`[syncToDauTay] hoantien-dautay trả lỗi ${res.status}: ${errText}`)
      return false
    }
    console.log(`[syncToDauTay] Đồng bộ ${type} thành công`)
    return true
  } catch (e) {
    console.warn(`[syncToDauTay] Không gửi được sang hoantien-dautay: ${e}`)
    return false
  }
}
