import type { SearchProgress } from '../types'

interface Props {
  progress: SearchProgress
}

export function Stats({ progress }: Props) {
  const counts = countByStatus(progress.results)
  const pct = progress.total ? Math.min(100, Math.round((progress.done / progress.total) * 100)) : 0

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-sm">
        <Pill label="claimed" value={counts.claimed} color="text-ok" />
        <Pill label="available" value={counts.free} color="text-zinc-400" />
        <Pill label="unknown" value={counts.unknown} color="text-warn" />
        <Pill label="error" value={counts.error} color="text-bad" />
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
  const out = { claimed: 0, free: 0, unknown: 0, error: 0, skipped: 0 }
  for (const r of results) out[r.status] = (out[r.status] ?? 0) + 1
  return out
}
