import { ApiError } from './types'
import type {
  AdminSiteEntry,
  AdminSitesResponse,
  CheckResult,
  GraphEdge,
  GraphExtracted,
  Member,
  Role,
  ScanDetail,
  ScanDiff,
  ScanSummary,
  SiteRaw,
  SitesResponse,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') ?? ''

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!r.ok) {
    const text = await r.text().catch(() => '')
    let detail = `HTTP ${r.status}`
    try {
      const j = JSON.parse(text)
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      if (text) detail = text
    }
    throw new ApiError(r.status, detail)
  }
  if (r.status === 204) return undefined as unknown as T
  return r.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------

export async function login(username: string, password: string): Promise<Member> {
  const body = await jsonFetch<{ member: Member }>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  return body.member
}

export async function logout(): Promise<void> {
  await jsonFetch<void>('/api/auth/logout', { method: 'POST' })
}

export async function fetchMe(): Promise<Member | null> {
  try {
    const body = await jsonFetch<{ member: Member }>('/api/auth/me')
    return body.member
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null
    throw e
  }
}

// ---------------------------------------------------------------------------
// admin
// ---------------------------------------------------------------------------

export async function listMembers(): Promise<Member[]> {
  const body = await jsonFetch<{ members: Member[] }>('/api/admin/members')
  return body.members
}

export async function createMember(payload: {
  username: string
  password: string
  role: Role
}): Promise<Member> {
  const body = await jsonFetch<{ member: Member }>('/api/admin/members', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return body.member
}

export async function deleteMember(username: string): Promise<void> {
  await jsonFetch<void>(`/api/admin/members/${encodeURIComponent(username)}`, {
    method: 'DELETE',
  })
}

export async function patchMember(
  username: string,
  patch: { role?: Role; password?: string },
): Promise<Member> {
  const body = await jsonFetch<{ member: Member }>(
    `/api/admin/members/${encodeURIComponent(username)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(patch),
    },
  )
  return body.member
}

// ---------------------------------------------------------------------------
// sites + search
// ---------------------------------------------------------------------------

export async function fetchSites(): Promise<SitesResponse> {
  return jsonFetch<SitesResponse>('/api/sites')
}

export interface SearchEvents {
  onStart?: (info: { username: string; total: number }) => void
  onResult?: (r: CheckResult) => void
  onDone?: (info: { results: number; cached?: boolean }) => void
  onError?: (err: string) => void
  signal?: AbortSignal
}

export async function streamSearch(
  payload: { username: string; tags: string[]; limit: number },
  events: SearchEvents,
): Promise<void> {
  const r = await fetch(`${API_BASE}/api/search`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
    signal: events.signal,
  })

  if (!r.ok) {
    const text = await r.text().catch(() => '')
    let detail = `HTTP ${r.status}`
    try {
      const j = JSON.parse(text)
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      if (text) detail = text
    }
    events.onError?.(detail)
    return
  }

  if (!r.body) {
    events.onError?.('streaming not supported')
    return
  }

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let idx
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const chunk = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      handleSseChunk(chunk, events)
    }
  }
  if (buffer.trim()) handleSseChunk(buffer, events)
}

// ---------------------------------------------------------------------------
// Phase 4: admin DB editor (live site catalog overrides)
// ---------------------------------------------------------------------------

export async function adminListSites(): Promise<AdminSitesResponse> {
  return jsonFetch<AdminSitesResponse>('/api/admin/sites')
}

export async function adminGetSite(name: string): Promise<AdminSiteEntry> {
  return jsonFetch<AdminSiteEntry>(`/api/admin/sites/${encodeURIComponent(name)}`)
}

export async function adminUpsertSite(payload: {
  name: string
  operation: 'upsert' | 'delete' | 'disable'
  patch: Partial<SiteRaw>
}): Promise<{ ok: boolean; site: AdminSiteEntry | null; catalog_size: number }> {
  return jsonFetch('/api/admin/sites', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function adminRemoveOverride(name: string): Promise<void> {
  await jsonFetch<void>(`/api/admin/sites/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export async function adminReloadCatalog(): Promise<{ ok: boolean; sites: number }> {
  return jsonFetch('/api/admin/sites/reload', { method: 'POST' })
}

// ---------------------------------------------------------------------------
// Phase 4: scan history (per-member, with diff)
// ---------------------------------------------------------------------------

export async function listHistory(filter?: {
  username?: string
  limit?: number
}): Promise<ScanSummary[]> {
  const qs = new URLSearchParams()
  if (filter?.username) qs.set('username', filter.username)
  if (filter?.limit) qs.set('limit', String(filter.limit))
  const path = qs.toString() ? `/api/history?${qs.toString()}` : '/api/history'
  const body = await jsonFetch<{ scans: ScanSummary[] }>(path)
  return body.scans
}

export async function getHistoryScan(scanId: number): Promise<ScanDetail> {
  return jsonFetch<ScanDetail>(`/api/history/${scanId}`)
}

export async function deleteHistoryScan(scanId: number): Promise<void> {
  await jsonFetch<void>(`/api/history/${scanId}`, { method: 'DELETE' })
}

export async function diffHistory(fromId: number, toId: number): Promise<ScanDiff> {
  return jsonFetch<ScanDiff>(`/api/history/diff/${fromId}/${toId}`)
}

function handleSseChunk(chunk: string, events: SearchEvents | GraphSearchEvents) {
  let event = 'message'
  const dataLines: string[] = []
  for (const raw of chunk.split('\n')) {
    if (raw.startsWith('event:')) event = raw.slice(6).trim()
    else if (raw.startsWith('data:')) dataLines.push(raw.slice(5).trim())
  }
  if (!dataLines.length) return
  let data: unknown
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }
  const e = events as GraphSearchEvents
  switch (event) {
    case 'start':
      e.onStart?.(data as { username: string; total: number; depth?: number })
      break
    case 'result':
      e.onResult?.(data as CheckResult)
      break
    case 'extracted':
      e.onExtracted?.(data as GraphExtracted)
      break
    case 'edge':
      e.onEdge?.(data as GraphEdge)
      break
    case 'secondary_start':
      e.onSecondaryStart?.(
        data as { parent_username: string; child_username: string; total: number },
      )
      break
    case 'done':
      e.onDone?.(
        data as { results: number; cached?: boolean; edges?: number; secondary_count?: number },
      )
      break
    case 'error':
      e.onError?.(String((data as { detail?: string }).detail ?? data))
      break
  }
}

// ---------------------------------------------------------------------------
// Phase 3: graph search (recursive identity discovery)
// ---------------------------------------------------------------------------

export interface GraphSearchEvents extends SearchEvents {
  onExtracted?: (e: GraphExtracted) => void
  onEdge?: (e: GraphEdge) => void
  onSecondaryStart?: (info: {
    parent_username: string
    child_username: string
    total: number
  }) => void
  onDone?: (info: {
    results: number
    cached?: boolean
    edges?: number
    secondary_count?: number
  }) => void
}

export async function streamGraphSearch(
  payload: {
    username: string
    tags: string[]
    limit: number
    depth?: number
    max_secondary?: number
  },
  events: GraphSearchEvents,
): Promise<void> {
  const r = await fetch(`${API_BASE}/api/search/graph`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
    signal: events.signal,
  })

  if (!r.ok) {
    const text = await r.text().catch(() => '')
    let detail = `HTTP ${r.status}`
    try {
      const j = JSON.parse(text)
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      if (text) detail = text
    }
    events.onError?.(detail)
    return
  }

  if (!r.body) {
    events.onError?.('streaming not supported')
    return
  }

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let idx
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const chunk = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      handleSseChunk(chunk, events)
    }
  }
  if (buffer.trim()) handleSseChunk(buffer, events)
}
