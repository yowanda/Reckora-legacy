export type Status = 'claimed' | 'free' | 'unknown' | 'blocked' | 'error' | 'skipped'

export interface CheckResult {
  site: string
  status: Status
  url: string
  url_main: string
  tags: string[]
  http_status: number | null
  elapsed_ms: number | null
  error: string | null
  // Phase 1 telemetry — backend emits these on every result. Older
  // backends (before Phase 1) may omit them; treat as missing.
  confidence?: number
  signals?: string[]
  attempts?: number
  // Phase 2: 2-stage verify outcome. ``true`` = re-probe with rotated
  // UA agreed; ``false`` = re-probe disagreed (status was demoted to
  // UNKNOWN); ``null``/missing = verify wasn't attempted.
  verified?: boolean | null
  // Phase 3: structured identity fields pulled from the page body for
  // confident CLAIMED hits.  ``null``/missing = extraction wasn't run
  // for this result.  ``{}`` = ran but no recognised fields.
  extracted?: Record<string, string> | null
  // Phase 3: graph-search annotations.  ``parent_username`` is the
  // username whose CLAIMED-hit extracts surfaced this sub-result;
  // ``null`` for primary scan results.  ``discovered_via`` repeats the
  // primary-side username to make the recursion edge easy to render.
  parent_username?: string | null
  discovered_via?: string
}

export interface GraphEdge {
  parent_username: string
  child_username: string
  evidence: string
  source_site: string
}

export interface GraphExtracted {
  parent_username: string
  site: string
  url: string
  fields: Record<string, string>
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

export interface GraphProgress {
  username: string
  state: 'idle' | 'running' | 'done' | 'error'
  error?: string
  results: CheckResult[]
  edges: GraphEdge[]
  extracted: GraphExtracted[]
  // Map of secondary username -> total sites being scanned (announced
  // via ``secondary_start``). Used by the UI to show which sub-scans
  // are still in-flight.
  secondaryScans: Record<string, number>
  doneCount: number
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
