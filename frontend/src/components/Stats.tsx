import type { CheckResult, InitialPdfReady, SearchProgress } from '../types'

interface Props {
  progress: SearchProgress
}

export function Stats({ progress }: Props) {
  const counts = countByStatus(progress.results)
  const filtered = countFiltered(progress.results)
  const pct = progress.total ? Math.min(100, Math.round((progress.done / progress.total) * 100)) : 0

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-sm">
        <Pill label="claimed" value={counts.claimed} color="text-ok" />
        <Pill label="available" value={counts.free} color="text-zinc-400" />
        <Pill label="unknown" value={counts.unknown} color="text-warn" />
        <Pill label="blocked" value={counts.blocked} color="text-purple-300" />
        <Pill label="error" value={counts.error} color="text-bad" />
        {filtered.link > 0 && (
          <Pill label="Filtered (link)" value={filtered.link} color="text-amber-400" />
        )}
        {filtered.confidence > 0 && (
          <Pill label="Filtered (confidence)" value={filtered.confidence} color="text-amber-400" />
        )}
        <span className="ml-auto text-xs text-zinc-500">
          {progress.done}/{progress.total} checked
          {progress.cached && <span className="ml-2 text-accent">cached</span>}
        </span>
      </div>
      <div className="h-1 w-full bg-bg-border">
        <div
          className={
            'h-full bg-gradient-to-r from-accent via-accent-soft to-ok transition-[width] ' +
            (progress.state === 'running' ? 'animate-pulse-soft' : '')
          }
          style={{ width: `${pct}%` }}
        />
      </div>
      {progress.initialPdf && <InitialPdfBanner pdf={progress.initialPdf} />}
    </div>
  )
}

// Phase 10a: rendered the moment the backend SSE channel announces the
// first qualifying CLAIMED hit. Gives the operator a one-click route to
// the preliminary PDF without waiting for the full scan to drain.
function InitialPdfBanner({ pdf }: { pdf: InitialPdfReady }) {
  const apiBase = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') ?? ''
  const downloadHref = `${apiBase}${pdf.url}`
  return (
    <div
      data-testid="initial-pdf-banner"
      className="flex flex-wrap items-center gap-3 border-t border-bg-border bg-ok/5 px-4 py-2 text-sm"
    >
      <span className="text-ok font-medium">First hit on {pdf.hit_site}</span>
      <span className="text-xs text-zinc-400">
        confidence {pdf.hit_confidence}
        {pdf.hit_verified ? ' · verified' : ''}
      </span>
      <a
        href={downloadHref}
        download
        target="_blank"
        rel="noreferrer"
        className="ml-auto inline-flex items-center rounded-md border border-ok/30 bg-ok/10 px-2.5 py-1 text-xs font-medium text-ok hover:bg-ok/20"
      >
        Download preliminary PDF
      </a>
    </div>
  )
}

function Pill({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className={`text-base font-semibold ${color}`}>{value}</span>
      <span className="text-xs uppercase tracking-wider text-zinc-500">{label}</span>
    </span>
  )
}

function countByStatus(results: SearchProgress['results']) {
  const out: Record<string, number> = {
    claimed: 0,
    free: 0,
    unknown: 0,
    blocked: 0,
    error: 0,
    skipped: 0,
  }
  for (const r of results) out[r.status] = (out[r.status] ?? 0) + 1
  return out as { claimed: number; free: number; unknown: number; blocked: number; error: number; skipped: number }
}

const LINK_DEMOTE_PREFIXES = [
  'link-dead-after-claim:',
  'link-redirects-login:',
  'link-redirects-home:',
  'display-url-dead:',
]

const CONFIDENCE_DEMOTE_PREFIXES = [
  'claimed-below-confidence-floor:',
  'claimed-below-ml-floor:',
  'claimed-not-verified',
]

function isDemotedBy(r: CheckResult, prefixes: string[]): boolean {
  if (r.status !== 'unknown' || !r.signals) return false
  return r.signals.some((s) => prefixes.some((p) => s.startsWith(p)))
}

function countFiltered(results: SearchProgress['results']) {
  let link = 0
  let confidence = 0
  for (const r of results) {
    if (isDemotedBy(r, LINK_DEMOTE_PREFIXES)) link++
    if (isDemotedBy(r, CONFIDENCE_DEMOTE_PREFIXES)) confidence++
  }
  return { link, confidence }
}
