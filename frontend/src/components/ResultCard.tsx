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

  return (
    <a
      href={r.url}
      target="_blank"
      rel="noreferrer noopener"
      className="card group flex animate-fade-in items-center gap-3 px-3 py-2.5 transition hover:border-accent/40 hover:bg-bg-card"
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
        <div className="flex items-center gap-2">
          <p className="truncate text-sm font-medium text-zinc-100">{r.site}</p>
          <span className={`chip border ${meta.className} !py-0`}>
            <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
            {meta.label}
          </span>
          {typeof r.confidence === 'number' && r.status !== 'skipped' && (
            <ConfidenceBadge value={r.confidence} status={r.status} />
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
        </div>
        <p className="truncate text-xs text-zinc-500">
          {host}
          {r.http_status && <span className="ml-2 font-mono">[{r.http_status}]</span>}
          {r.elapsed_ms != null && <span className="ml-2">{r.elapsed_ms}ms</span>}
          {r.attempts != null && r.attempts > 1 && (
            <span className="ml-2 font-mono text-warn">retry×{r.attempts - 1}</span>
          )}
        </p>
      </div>
      <ExternalIcon className="hidden h-4 w-4 text-zinc-500 transition group-hover:text-accent sm:block" />
    </a>
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

function ExternalIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  )
}
