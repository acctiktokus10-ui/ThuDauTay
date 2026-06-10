// pages/api/status.js
// Trả về thời gian cập nhật cuối của từng loại dữ liệu

let _store = {}

async function kvGet(key) {
  if (process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN) {
    const url = `${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(key)}`
    const res = await fetch(url, {
      headers: {
        Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`,
      },
    })
    if (!res.ok) return null
    const json = await res.json()
    let result = json.result ?? null
    if (typeof result === 'string') { try { result = JSON.parse(result) } catch { return null } }
    return result
  }
  const raw = _store[key] ?? null
  if (typeof raw === 'string') { try { return JSON.parse(raw) } catch { return null } }
  return raw
}

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' })
  }

  const donhangMeta = await kvGet('meta_donhang')
  const vitienMeta = await kvGet('meta_vitien')

  return res.status(200).json({
    donhang: donhangMeta || { updated_at: null, count: 0 },
    vitien: vitienMeta || { updated_at: null, count: 0 },
  })
}
