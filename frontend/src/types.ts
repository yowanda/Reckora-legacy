export type Status = 'claimed' | 'free' | 'unknown' | 'error' | 'skipped'

export interface CheckResult {
  site: string
  status: Status
  url: string
  url_main: string
  tags: string[]
  http_status: number | null
  elapsed_ms: number | null
  error: string | null
}

export interface SiteSummary {
  name: string
  url_main: string
  tags: string[]
  alexa_rank: number | null
}

export interface TagSummary {
  name: string
  count: number
}

export interface SitesResponse {
  count: number
  tags: TagSummary[]
  sites: SiteSummary[]
}

export interface SearchProgress {
  total: number
  done: number
  results: CheckResult[]
  state: 'idle' | 'running' | 'done' | 'error'
  error?: string
  cached?: boolean
}

export type Role = 'admin' | 'member'

export interface Member {
  username: string
  role: Role
  created_at: number
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}
