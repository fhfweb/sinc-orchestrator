import { useAppStore } from '@/stores/app'

export function useApi() {
  const app = useAppStore()

  async function api<T = unknown>(
    path: string,
    opts: RequestInit & { params?: Record<string, string | number | boolean | undefined> } = {}
  ): Promise<T> {
    const { params, ...fetchOpts } = opts
    let url = `${app.apiBase}${path}`

    const q = new URLSearchParams()
    q.set('tenant_id', app.tenantId)
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined) q.set(k, String(v))
      }
    }
    url += '?' + q.toString()

    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(fetchOpts.headers ?? {}) },
      ...fetchOpts
    })

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      throw new Error(`API ${path} → ${res.status}: ${text}`)
    }

    const ct = res.headers.get('content-type') ?? ''
    if (ct.includes('application/json')) return res.json() as Promise<T>
    return res.text() as unknown as T
  }

  async function apif<T = unknown>(path: string, body: unknown): Promise<T> {
    return api<T>(path, {
      method: 'POST',
      body: JSON.stringify(body)
    })
  }

  return { api, apif }
}
