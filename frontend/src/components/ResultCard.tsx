import { useState } from 'react'
import type { CheckResult, Status } from '../types'

const statusMeta: Record<Status, { label: string; className: string; dot: string }> = {
  claimed: { label: 'claimed', className: 'border-ok/40 bg-ok/10 text-ok', dot: 'bg-ok' },
  free: { label: 'available', className: 'border-zinc-700 bg-zinc-800/30 text-zinc-400', dot: 'bg-zinc-500' },
  unknown: { label: 'unknown', className: 'border-warn/40 bg-warn/10 text-warn', dot: 'bg-warn' },
  blocked: { label: 'blocked', className: 'border-purple-500/40 bg-purple-500/10 text-purple-300', dot: 'bg-purple-400' },
  error: { label: 'error', className: 'border-bad/40 bg-bad/10 text-bad', dot: 'bg-bad' },
  skipped: { label: 'skipped', className: 'border-zinc-800 bg-zinc-900/40 text-zinc-500', dot: 'bg-zinc-600' },
}

export function ResultCard({ r }: { r: CheckResult }) {
  const meta = statusMeta[r.status]
  const host = safeHost(r.url_main)
  const favicon = host ? `https://www.google.com/s2/favicons?domain=${host}&sz=64` : ''
  const [showExtracted, setShowExtracted] = useState(false)
  const extractedEntries = r.extracted ? Object.entries(r.extracted) : []
  const hasExtracted = extractedEntries.length > 0

  return (
    <div className="card group animate-fade-in transition hover:border-accent/40 hover:bg-bg-card">
      <a
        href={r.url}
        target="_blank"
        rel="noreferrer noopener"
        className="flex items-center gap-3 px-3 py-2.5"
      >
        {favicon ? (
          <img
            src={favicon}
            alt=""
            width={24}
            height={24}
            loading="lazy"
            className="h-6 w-6 flex-none rounded bg-zinc-800"
            onError={(e) => ((e.currentTarget.style.visibility = 'hidden'))}
          />
        ) : (
          <div className="h-6 w-6 flex-none rounded bg-zinc-800" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="truncate text-sm font-medium text-zinc-100">{r.site}</p>
            <span className={`chip border ${meta.className} !py-0`}>
              <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
              {meta.label}
            </span>
            {typeof r.confidence === 'number' && r.status !== 'skipped' && (
              <ConfidenceBadge value={r.confidence} status={r.status} />
            )}
            {typeof r.ml_confidence === 'number' && r.status !== 'skipped' && (
              <MlBadge value={r.ml_confidence} ruleConfidence={r.confidence} status={r.status} />
            )}
            {r.signals?.some((s) => s.startsWith('ml-disagree:')) && (
              <span
                className="chip border border-warn/40 bg-warn/10 !py-0 text-[10px] text-warn"
                title="Phase 4 ML model disagrees with the rule-based classifier — manual review suggested."
              >
                ! ml-disagree
              </span>
            )}
            {r.verified === true && (
              <span
                className="chip border border-ok/40 bg-ok/10 !py-0 text-[10px] text-ok"
                title="Confirmed by a second probe with a rotated UA + referer."
              >
                ✓ verified
              </span>
            )}
            {r.verified === false && (
              <span
                className="chip border border-warn/40 bg-warn/10 !py-0 text-[10px] text-warn"
                title="Second probe disagreed; primary classification demoted."
              >
                ! conflict
              </span>
            )}
            {r.signals?.includes('link-verified-live') && (
              <span
                className="chip border border-ok/40 bg-ok/10 !py-0 text-[10px] text-ok"
                title="Phase 5h: link liveness probe confirmed a real profile page behind this URL."
                data-testid="badge-link-live"
              >
                Link verified live
              </span>
            )}
            {r.status === 'unknown' &&
              r.signals?.some(
                (s) =>
                  s.startsWith('link-dead-after-claim:') ||
                  s.startsWith('link-redirects-login:') ||
                  s.startsWith('link-redirects-home:') ||
                  s.startsWith('display-url-dead:'),
              ) && (
                <span
                  className="chip border border-warn/40 bg-warn/10 !py-0 text-[10px] text-warn"
                  title="Phase 5h: link liveness probe found the display URL is dead, redirected, or a soft-404."
                  data-testid="badge-link-dead"
                >
                  Demoted (link dead)
                </span>
              )}
            {hasExtracted && (
              <button
                type="button"
                className="chip border border-accent/40 bg-accent/10 !py-0 text-[10px] text-accent hover:bg-accent/20"
                title={`Phase 3 extracted ${extractedEntries.length} identifier(s) from this page.`}
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  setShowExtracted((v) => !v)
                }}
              >
                ↗ extracted · {extractedEntries.length}
              </button>
            )}
          </div>
          <p className="truncate text-xs text-zinc-500">
            {host}
            {r.http_status && <span className="ml-2 font-mono">[{r.http_status}]</span>}
            {r.elapsed_ms != null && <span className="ml-2">{r.elapsed_ms}ms</span>}
            {r.attempts != null && r.attempts > 1 && (
              <span className="ml-2 font-mono text-warn">retry×{r.attempts - 1}</span>
            )}
            {r.discovered_via && (
              <span
                className="ml-2 font-mono text-emerald-400"
                title="Discovered via Phase 3 graph recursion."
              >
                via {r.discovered_via}
              </span>
            )}
          </p>
        </div>
        <ExternalIcon className="hidden h-4 w-4 text-zinc-500 transition group-hover:text-accent sm:block" />
      </a>
      {hasExtracted && showExtracted && (
        <div className="border-t border-bg-border bg-bg-card/50 px-3 py-2 text-xs">
          {extractedEntries.map(([k, v]) => (
            <div key={k} className="flex gap-2 py-0.5">
              <span className="w-24 flex-none truncate font-mono text-zinc-500">{k}</span>
              <span className="min-w-0 flex-1 break-all text-zinc-200">{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function safeHost(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return ''
  }
}

function ConfidenceBadge({ value, status }: { value: number; status: Status }) {
  // Color the badge with the status color when confidence is high; fade it
  // toward neutral when confidence is low so the user can see at a glance
  // which classifications need a second look.
  let color = 'text-zinc-400'
  if (status === 'claimed') color = value >= 80 ? 'text-ok' : value >= 60 ? 'text-warn' : 'text-zinc-400'
  else if (status === 'free') color = value >= 80 ? 'text-zinc-300' : 'text-zinc-500'
  else if (status === 'blocked') color = 'text-purple-300'
  else if (status === 'unknown') color = 'text-warn'
  return (
    <span
      className={`chip border border-zinc-800 bg-zinc-900/40 !py-0 font-mono text-[10px] ${color}`}
      title={`Confidence ${value}/100`}
    >
      {value}
    </span>
  )
}

function MlBadge({
  value,
  ruleConfidence,
  status,
}: {
  value: number
  ruleConfidence?: number
  status: Status
}) {
  // Phase 4: indigo-tinted complement to the rule-confidence chip. The
  // model is independent of the rule classifier, so showing both makes
  // disagreements obvious without forcing one to win.
  const tone =
    status === 'claimed'
      ? value >= 70
        ? 'text-indigo-300'
        : 'text-warn'
      : status === 'free'
        ? value <= 30
          ? 'text-indigo-300'
          : 'text-warn'
        : 'text-indigo-300'
  const title =
    typeof ruleConfidence === 'number'
      ? `ML probability ${value}/100 (rule ${ruleConfidence}/100)`
      : `ML probability ${value}/100`
  return (
    <span
      className={`chip border border-indigo-500/30 bg-indigo-500/5 !py-0 font-mono text-[10px] ${tone}`}
      title={title}
    >
      ml {value}
    </span>
  )
}

function ExternalIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  )
}
