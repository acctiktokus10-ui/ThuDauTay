// pages/api/reload.js
// Web bấm nút → POST /api/reload → set cờ pending=true
// Bot poll GET /api/reload → nếu pending → load data → ghi kết quả lại → web poll thấy kết quả

const PASSWORD = process.env.UPLOAD_PASSWORD || 'Thucute9999'
let _store = {}

async function kvGet(key) {
  if (process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN) {
    const url = `${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(key)}`
    const res = await fetch(url, { headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}` } })
    if (!res.ok) return null
    const json = await res.json()
    let result = json.result ?? null
    // Parse nhiều lần cho đến khi ra object (tránh double-stringify)
    while (typeof result === 'string') {
      try { result = JSON.parse(result) } catch { break }
    }
    return result
  }
  const raw = _store[key] ?? null
  let result = raw
  while (typeof result === 'string') {
    try { result = JSON.parse(result) } catch { break }
  }
  return result
}

async function kvSet(key, value) {
  if (process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN) {
    const url = `${process.env.UPSTASH_REDIS_REST_URL}/set/${encodeURIComponent(key)}`
    const res = await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(typeof value === 'string' ? value : JSON.stringify(value)),
    })
    return res.ok
  }
  _store[key] = typeof value === 'string' ? value : JSON.stringify(value)
  return true
}

export default async function handler(req, res) {

  // POST — web bấm nút → set cờ reload + xóa kết quả cũ
  if (req.method === 'POST') {
    const { password, poll_result, pending } = req.body

    // Bot ghi kết quả reload lên (internal call, không cần password)
    if (poll_result !== undefined) {
      await kvSet('reload_status', poll_result)
      return res.status(200).json({ success: true })
    }

    // [FIX] Bot reset cờ pending sau khi load xong (internal call, không cần password)
    if (pending === false) {
      await kvSet('reload_flag', { pending: false, reset_at: new Date().toISOString() })
      return res.status(200).json({ success: true })
    }

    if (password !== PASSWORD) return res.status(401).json({ error: 'Sai mật khẩu' })

    // Xóa kết quả cũ, set cờ pending
    await kvSet('reload_status', { state: 'pending', requested_at: new Date().toISOString() })
    await kvSet('reload_flag', { pending: true, requested_at: new Date().toISOString() })
    return res.status(200).json({ success: true, message: 'Đã gửi lệnh tải dữ liệu cho bot' })
  }

  // GET — bot poll cờ (không reset ngay, bot tự reset sau khi load xong)
  if (req.method === 'GET') {
    const { poll_status } = req.query

    // Web poll kết quả (poll_status=1)
    if (poll_status === '1') {
      const status = await kvGet('reload_status')
      return res.status(200).json(status || { state: 'unknown' })
    }

    // Bot poll cờ
    const flag = await kvGet('reload_flag')
    console.log('[reload GET] flag from Redis:', JSON.stringify(flag))
    // pending có thể là boolean true hoặc string "true" tùy Redis serialize
    const isPending = flag && (flag.pending === true || flag.pending === 'true')
    if (isPending) {
      // KHÔNG reset ngay — bot tự reset sau khi load xong thành công
      return res.status(200).json({ reload: true })
    }
    return res.status(200).json({ reload: false, flag: flag })
  }

  return res.status(405).json({ error: 'Method not allowed' })
}
