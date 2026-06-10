// pages/api/data/[type].js
// Bot gọi GET /api/data/donhang|vitien|danhan để lấy dữ liệu
// Bot gọi POST /api/data/danhan để ghi lại sau khi rút tiền

let _store = {}

async function kvGet(key) {
  if (process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN) {
    const url = `${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(key)}`
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}` },
    })
    if (!res.ok) return null
    const json = await res.json()
    let result = json.result ?? null
    // Redis luôn trả về string — parse ra object nếu cần
    if (typeof result === 'string') {
      try { result = JSON.parse(result) } catch { return null }
    }
    return result
  }
  const raw = _store[key] ?? null
  if (typeof raw === 'string') { try { return JSON.parse(raw) } catch { return null } }
  return raw
}

async function kvSet(key, value) {
  if (process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN) {
    const url = `${process.env.UPSTASH_REDIS_REST_URL}/set/${encodeURIComponent(key)}`
    // Luôn serialize thành string → nhất quán với kvGet
    const valueStr = typeof value === 'string' ? value : JSON.stringify(value)
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(valueStr),
    })
    return res.ok
  }
  _store[key] = typeof value === 'string' ? value : JSON.stringify(value)
  return true
}

const VALID_TYPES = ['donhang', 'vitien', 'danhan']
const BOT_SECRET = process.env.BOT_SECRET || ''

export default async function handler(req, res) {
  const { type } = req.query
  if (!VALID_TYPES.includes(type)) {
    return res.status(400).json({ error: 'type phải là donhang, vitien hoặc danhan' })
  }

  // GET — bot đọc dữ liệu
  if (req.method === 'GET') {
    const key = `${type}_by_subid`
    let data = await kvGet(key)
    if (data === null) {
      // Trả về object rỗng thay vì 404 — bot fallback về local
      return res.status(200).json({})
    }
    // Redis lưu JSON dưới dạng string → parse ra object nếu cần
    if (typeof data === 'string') {
      try { data = JSON.parse(data) } catch { return res.status(200).json({}) }
    }
    res.setHeader('Cache-Control', 'no-store')
    return res.status(200).json(data)
  }

  // POST — chỉ danhan được ghi (bot ghi sau khi rút tiền)
  if (req.method === 'POST') {
    if (type !== 'danhan') {
      return res.status(405).json({ error: 'Chỉ danhan mới được ghi qua POST' })
    }

    // Xác thực bot secret nếu có cài
    if (BOT_SECRET) {
      const secret = req.headers['x-bot-secret']
      if (secret !== BOT_SECRET) {
        return res.status(401).json({ error: 'Không có quyền ghi dữ liệu' })
      }
    }

    const { data } = req.body
    if (!data || typeof data !== 'object') {
      return res.status(400).json({ error: 'data không hợp lệ' })
    }

    const ok = await kvSet('danhan_by_subid', data)
    if (!ok) return res.status(500).json({ error: 'Lưu thất bại' })

    // Cập nhật metadata
    await kvSet('meta_danhan', { updated_at: new Date().toISOString(), count: Object.keys(data).length })

    return res.status(200).json({ success: true })
  }

  return res.status(405).json({ error: 'Method not allowed' })
}
