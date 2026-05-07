export type Status = 'claimed' | 'free' | 'unknown' | 'blocked' | 'error' | 'skipped'

// ---------------------------------------------------------------------------
// Phase 5a: identity types. Mirrors ``backend/app/identity.py``. The
// SPA's picker swaps the search input's placeholder + ``pattern``
// attribute based on the active value; the backend re-validates the
// pair on every search request.
// ---------------------------------------------------------------------------

export type IdentityType = 'username' | 'email' | 'phone' | 'gaia' | 'yandex' | 'skype'

// ---------------------------------------------------------------------------
// Phase 5g: scan depth (how many sites a single search probes).
// Pre-5g the SPA hard-coded ``limit: 250`` for every stream, so users had
// no way to scan beyond the top 250 alexa-ranked sites — the remaining
// ~2,256 niche / regional / new sites in the catalogue silently never
// got probed. The picker below exposes a small set of presets;
// ``ALL_SITES`` resolves to the catalog count at runtime.
// ---------------------------------------------------------------------------

export type ScanDepth = 100 | 250 | 500 | 1000 | 'all'

export const SCAN_DEPTH_PRESETS: readonly ScanDepth[] = [
  100,
  250,
  500,
  1000,
  'all',
] as const

// Hard cap mirrored from ``backend/app/main.py``::``SearchRequest.limit``
// (Phase 5g raised the backend cap from 600 → 3,000). Used as the
// fallback when the catalog metadata hasn't loaded yet.
export const SCAN_DEPTH_HARD_CAP = 3000

export const DEFAULT_SCAN_DEPTH: ScanDepth = 250

export interface ScanDepthMeta {
  key: ScanDepth
  label: string
  // Approximate p95 wall-clock from Phase-2 benchmark, scaled linearly.
  // Used for the "(~Xs)" hint on the picker. Real number depends on the
  // active proxy + how many sites are BLOCKED.
  estimated_seconds: number
}

export const SCAN_DEPTH_META: Record<string, ScanDepthMeta> = {
  '100': { key: 100, label: 'Top 100', estimated_seconds: 15 },
  '250': { key: 250, label: 'Top 250', estimated_seconds: 30 },
  '500': { key: 500, label: 'Top 500', estimated_seconds: 65 },
  '1000': { key: 1000, label: 'Top 1000', estimated_seconds: 130 },
  all: { key: 'all', label: 'All sites', estimated_seconds: 330 },
}

// Resolve a ``ScanDepth`` preset to a concrete integer the backend
// accepts. ``'all'`` resolves to the live catalog count (clamped to the
// backend hard cap so a future regen doesn't accidentally exceed it).
export function resolveScanDepth(
  depth: ScanDepth,
  catalogCount: number | undefined,
): number {
  if (depth === 'all') {
    const total = catalogCount && catalogCount > 0 ? catalogCount : SCAN_DEPTH_HARD_CAP
    return Math.min(total, SCAN_DEPTH_HARD_CAP)
  }
  return depth
}

export const IDENTITY_TYPES: readonly IdentityType[] = [
  'username',
  'email',
  'phone',
  'gaia',
  'yandex',
  'skype',
] as const

export interface IdentityTypeMeta {
  key: IdentityType
  label: string
  placeholder: string
  pattern: string // RegExp source mirrored from backend identity.py
  hint: string
}

export const IDENTITY_TYPE_META: Record<IdentityType, IdentityTypeMeta> = {
  username: {
    key: 'username',
    label: 'username',
    placeholder: 'enter a username, e.g. github, torvalds, jack',
    pattern: '^[A-Za-z0-9_.-]{1,64}$',
    hint: 'letters, digits, _ . - (max 64 chars)',
  },
  email: {
    key: 'email',
    label: 'email',
    placeholder: 'enter an email, e.g. alice@example.com',
    pattern: '^[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}$',
    hint: 'RFC 5321 addr-spec — local@domain.tld',
  },
  phone: {
    key: 'phone',
    label: 'phone',
    placeholder: 'enter a phone in E.164, e.g. +12025551234',
    pattern: '^\\+[1-9][0-9]{6,14}$',
    hint: 'E.164: leading + then 7-15 digits, no spaces',
  },
  gaia: {
    key: 'gaia',
    label: 'GAIA id',
    placeholder: 'enter a 21-digit Google account id',
    pattern: '^[0-9]{21}$',
    hint: 'Google Account / GAIA — exactly 21 digits',
  },
  yandex: {
    key: 'yandex',
    label: 'Yandex id',
    placeholder: 'enter a 16-char Yandex public_id',
    pattern: '^[a-z0-9]{16}$',
    hint: 'Yandex public_id — 16 lowercase alphanumerics',
  },
  skype: {
    key: 'skype',
    label: 'Skype',
    placeholder: 'enter a Skype name, e.g. live:johndoe',
    pattern: '^(?:live:)?[A-Za-z0-9_.\\-]{6,32}$',
    hint: 'Skype name — 6-32 chars, optional ``live:`` prefix',
  },
}

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

// Phase 7a: an edge stitched between two CLAIMED ``site:`` nodes when
// their avatar perceptual hashes are within ``image_phash_max_distance``
// bits of each other. ``parent_username_*`` is the username under which
// each side of the pair was discovered (the root for primary CLAIMED
// hits; the child handle for secondary scans).
export interface GraphPhashEdge {
  site_a: string
  site_b: string
  parent_username_a: string | null
  parent_username_b: string | null
  hamming: number
  max_distance: number
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
  // Phase 7a: avatar-pHash-derived stitches between CLAIMED hits.
  // Empty when ``enable_image_phash`` is off (the default).
  phashEdges: GraphPhashEdge[]
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

// ---------------------------------------------------------------------------
// Phase 7b: WHOIS / HIBP-style enrichment for ``identity_type=email``.
// Mirrors ``backend/app/enrichment.py``'s ``WhoisRecord``, ``Breach`` and
// the ``/api/enrich/email`` envelope shape.
// ---------------------------------------------------------------------------

export interface WhoisRecord {
  domain: string
  registrar: string | null
  created: string | null
  expires: string | null
  updated: string | null
  nameservers: string[]
  status: string[]
}

export interface Breach {
  name: string
  title: string
  domain: string | null
  breach_date: string | null
  added_date: string | null
  pwn_count: number | null
  data_classes: string[]
  is_verified: boolean
  is_fabricated: boolean
  is_sensitive: boolean
}

export interface EnrichmentResult {
  email: string
  domain: string | null
  whois: WhoisRecord | null
  breaches: Breach[] | null
  breaches_disabled: boolean
}

export interface EnrichmentResponse {
  disabled: boolean
  rate_limited: boolean
  result: EnrichmentResult | null
}

// Phase 7c: reverse-image lookup hook.
export interface ReverseImageProvider {
  slug: string
  label: string
  url: string
}

export interface ReverseImageResponse {
  disabled: boolean
  providers: ReverseImageProvider[]
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}
