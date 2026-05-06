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
  // Phase 5c: when set, the result was produced by a permutation scan
  // (one of the variants generated for the base handle). For base
  // results from list/graph searches, this field is absent.
  permutation_username?: string
  // Phase 4: complementary 0-100 probability from the bundled ONNX
  // classifier. ``null``/missing = ML scoring wasn't run (model
  // unavailable, feature disabled, or a SKIPPED probe).
  ml_confidence?: number | null
}

// ---------------------------------------------------------------------------
// Phase 4: admin DB editor + scan history.
// ---------------------------------------------------------------------------

export interface SiteRaw {
  url: string
  urlMain?: string
  checkType?: string
  regexCheck?: string
  absenceStrs?: string[]
  presenseStrs?: string[]
  headers?: Record<string, string>
  errors?: Record<string, string>
  alexaRank?: number | null
  tags?: string[]
  requestHeadOnly?: boolean
  ignore403?: boolean
  engine?: string
  prewarm?: boolean
  disabled?: boolean
}

export type SiteSource = 'base' | 'override' | 'custom'

export interface AdminSiteEntry {
  name: string
  source: SiteSource
  disabled: boolean
  engine: string | null
  tags: string[]
  raw: SiteRaw
  override?: {
    name: string
    operation: 'upsert' | 'delete' | 'disable'
    patch: Record<string, unknown>
    created_by: string
    created_at: number
    updated_at: number
  }
}

export interface AdminSitesResponse {
  sites: AdminSiteEntry[]
  total: number
  custom_count: number
  overridden_count: number
  disabled_count: number
}

// ---------------------------------------------------------------------------
// Phase 5d: admin-tunable runtime knobs (graph fanout + extractor floor).
// ---------------------------------------------------------------------------

export type AdminKnobKey =
  | 'graph_max_depth'
  | 'graph_max_secondary'
  | 'graph_max_seed_hits'
  | 'extract_min_confidence'
  | 'proxy_enabled'

export interface AdminKnob {
  key: AdminKnobKey
  description: string
  minimum: number
  maximum: number
  default: number
  override: number | null
  effective: number
}

export interface AdminKnobsResponse {
  knobs: AdminKnob[]
}

export type EffectiveSettings = Record<AdminKnobKey, number>

// ---------------------------------------------------------------------------
// Phase 5e: outbound proxy state. The descriptor never carries the
// proxy URL itself — only its scheme + on/off state — so the SPA can
// render a small "via tor" / "via http proxy" badge without leaking
// infrastructure details.
// ---------------------------------------------------------------------------

export interface ProxyStatus {
  configured: boolean
  enabled: boolean
  active: boolean
  scheme: string
}

export interface PublicSettingsResponse {
  settings: EffectiveSettings
  proxy: ProxyStatus
}

export interface AdminKnobsPatch {
  graph_max_depth?: number | null
  graph_max_secondary?: number | null
  graph_max_seed_hits?: number | null
  extract_min_confidence?: number | null
  proxy_enabled?: number | null
  reset?: AdminKnobKey[]
}

export interface ScanSummary {
  id: number
  member: string
  username: string
  mode: 'list' | 'graph'
  started_at: number
  finished_at: number
  duration_ms: number
  site_count: number
  claimed: number
  free: number
  blocked: number
  unknown: number
  error_count: number
}

export interface ScanResultRow {
  site: string
  status: Status
  confidence: number | null
  ml_confidence: number | null
  url: string | null
  parent_username: string | null
}

export interface ScanDetail {
  scan: ScanSummary
  results: ScanResultRow[]
}

export interface ScanDiff {
  from: ScanSummary
  to: ScanSummary
  newly_claimed: ScanResultRow[]
  no_longer_claimed: ScanResultRow[]
  status_changed: { site: string; from: Status | null; to: Status | null }[]
  confidence_shift: {
    site: string
    status: Status
    from_confidence: number
    to_confidence: number
  }[]
  unchanged_count: number
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
