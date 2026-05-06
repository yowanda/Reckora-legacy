import { useEffect, useMemo, useState } from 'react'
import {
  deleteHistoryScan,
  diffHistory,
  downloadHistoryExport,
  getHistoryScan,
  listHistory,
  type ExportFormat,
} from '../api'
import {
  ApiError,
  type ScanDetail,
  type ScanDiff,
  type ScanSummary,
  type Status,
} from '../types'

// Phase 5b: visible export formats in the scan modal. Order matters
// here — the first option ("HTML") is what most operators want
// (browser-printable). PDF is generated client-side via the browser's
// print dialog after opening the HTML export.
const EXPORT_OPTIONS: { fmt: ExportFormat; label: string; hint: string }[] = [
  { fmt: 'html', label: 'HTML', hint: 'Browser-printable (Save as PDF via Print)' },
  { fmt: 'csv', label: 'CSV', hint: 'Full results as a spreadsheet' },
  { fmt: 'json', label: 'JSON', hint: 'Raw payload for programmatic use' },
  { fmt: 'markdown', label: 'Markdown', hint: 'Compact summary for tickets/changelog' },
]

/**
 * Phase 4: per-member scan history with side-by-side diff viewer.
 *
 * The list view shows the caller's last N scans; selecting two checkboxes
 * enables a diff button that POSTs to ``/api/history/diff/<from>/<to>``.
 */
export function HistoryView() {
  const [scans, setScans] = useState<ScanSummary[]>([])
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<ScanDetail | null>(null)
  const [diff, setDiff] = useState<ScanDiff | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())

  useEffect(() => {
    void load()
  }, [])

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setScans(await listHistory({ limit: 50 }))
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setLoading(false)
    }
  }

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return scans
    return scans.filter((s) => s.username.toLowerCase().includes(q))
  }, [scans, filter])

  function toggleSelect(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        if (next.size >= 2) {
          // keep only the most recent click + the new one
          const arr = Array.from(next)
          next.delete(arr[0])
        }
        next.add(id)
      }
      return next
    })
  }

  async function onOpen(s: ScanSummary) {
    try {
      setOpen(await getHistoryScan(s.id))
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  async function onDiff() {
    const ids = Array.from(selected).sort((a, b) => a - b)
    if (ids.length !== 2) return
    setError(null)
    try {
      setDiff(await diffHistory(ids[0], ids[1]))
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  async function onDelete(s: ScanSummary) {
    if (!confirm(`Delete scan #${s.id} for "${s.username}"?`)) return
    try {
      await deleteHistoryScan(s.id)
      setSelected((prev) => {
        const n = new Set(prev)
        n.delete(s.id)
        return n
      })
      await load()
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  return (
    <section className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold tracking-tight">My scan history</h3>
          <p className="mt-1 text-xs text-zinc-400">
            Last 50 scans you've run. Pick two rows to diff (added/removed CLAIMED, status drift).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost text-xs" onClick={() => void load()}>
            Refresh
          </button>
          <button
            className="btn-primary text-xs"
            disabled={selected.size !== 2}
            onClick={() => void onDiff()}
          >
            Diff selected
          </button>
        </div>
      </header>

      {error && (
        <div className="card border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">{error}</div>
      )}

      <input
        className="input w-full max-w-md"
        placeholder="filter by username…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />

      {loading ? (
        <p className="text-sm text-zinc-400">Loading history…</p>
      ) : filtered.length === 0 ? (
        <p className="text-sm text-zinc-500">
          No scans recorded yet. Run a search and they'll show up here.
        </p>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 text-left text-xs uppercase tracking-wider text-zinc-500">
              <tr>
                <th className="w-8 px-3 py-2"></th>
                <th className="px-3 py-2">Username</th>
                <th className="px-3 py-2">When</th>
                <th className="px-3 py-2">Mode</th>
                <th className="px-3 py-2">Sites</th>
                <th className="px-3 py-2">CLAIMED</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((s) => (
                <tr key={s.id} className="border-t border-zinc-800">
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(s.id)}
                      onChange={() => toggleSelect(s.id)}
                    />
                  </td>
                  <td className="px-3 py-2 font-medium">{s.username}</td>
                  <td className="px-3 py-2 text-xs text-zinc-400">
                    {new Date(s.finished_at * 1000).toLocaleString()}
                  </td>
                  <td className="px-3 py-2 text-xs text-zinc-400">{s.mode}</td>
                  <td className="px-3 py-2 text-xs text-zinc-400">{s.site_count}</td>
                  <td className="px-3 py-2 text-xs text-emerald-300">{s.claimed}</td>
                  <td className="px-3 py-2 text-right">
                    <button className="btn-ghost text-xs" onClick={() => void onOpen(s)}>
                      View
                    </button>
                    <button
                      className="btn-ghost text-xs text-bad"
                      onClick={() => void onDelete(s)}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <ScanModal detail={open} onClose={() => setOpen(null)} />
      )}
      {diff && <DiffModal diff={diff} onClose={() => setDiff(null)} />}
    </section>
  )
}

function ScanModal({ detail, onClose }: { detail: ScanDetail; onClose: () => void }) {
  const [exporting, setExporting] = useState<ExportFormat | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)

  async function onExport(fmt: ExportFormat) {
    setExporting(fmt)
    setExportError(null)
    try {
      await downloadHistoryExport(detail.scan.id, fmt)
    } catch (e) {
      setExportError(humanizeError(e))
    } finally {
      setExporting(null)
    }
  }

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="card max-h-[80vh] w-full max-w-2xl space-y-3 overflow-auto px-5 py-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold tracking-tight">
          Scan #{detail.scan.id} — {detail.scan.username}
        </h3>
        <p className="text-xs text-zinc-400">
          {new Date(detail.scan.finished_at * 1000).toLocaleString()} · {detail.scan.mode} · {detail.scan.site_count} sites · {detail.scan.claimed} CLAIMED
        </p>
        <div className="flex flex-wrap items-center gap-2 border-t border-b border-zinc-800 py-2 text-xs">
          <span className="text-zinc-500">Export:</span>
          {EXPORT_OPTIONS.map((opt) => (
            <button
              key={opt.fmt}
              type="button"
              className="btn-ghost text-xs"
              disabled={exporting !== null}
              title={opt.hint}
              onClick={() => void onExport(opt.fmt)}
            >
              {exporting === opt.fmt ? '…' : opt.label}
            </button>
          ))}
        </div>
        {exportError && (
          <p className="text-xs text-bad" role="alert">
            {exportError}
          </p>
        )}
        <div className="space-y-1 text-xs">
          {detail.results
            .filter((r) => r.status === 'claimed')
            .map((r) => (
              <div
                key={r.site}
                className="flex items-center justify-between border-b border-zinc-800 py-1"
              >
                <span className="font-medium">{r.site}</span>
                <span className="flex items-center gap-2 text-zinc-400">
                  {r.url && (
                    <a
                      href={r.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="link"
                    >
                      open
                    </a>
                  )}
                  <span>conf {r.confidence ?? '–'}</span>
                  {typeof r.ml_confidence === 'number' && <span>ml {r.ml_confidence}</span>}
                </span>
              </div>
            ))}
        </div>
        <div className="text-right">
          <button className="btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

function DiffModal({ diff, onClose }: { diff: ScanDiff; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="card max-h-[85vh] w-full max-w-3xl space-y-4 overflow-auto px-5 py-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold tracking-tight">
          Diff — scan #{diff.from.id} → #{diff.to.id}
        </h3>
        <p className="text-xs text-zinc-400">
          {diff.from.username}: {new Date(diff.from.finished_at * 1000).toLocaleString()} →{' '}
          {new Date(diff.to.finished_at * 1000).toLocaleString()}
        </p>

        <DiffSection title="Newly CLAIMED" rows={diff.newly_claimed.length} accent="text-emerald-300">
          {diff.newly_claimed.map((r) => (
            <DiffRow key={r.site} site={r.site} url={r.url} status={r.status} />
          ))}
        </DiffSection>

        <DiffSection title="No longer CLAIMED" rows={diff.no_longer_claimed.length} accent="text-rose-300">
          {diff.no_longer_claimed.map((r) => (
            <DiffRow key={r.site} site={r.site} url={r.url} status={r.status} />
          ))}
        </DiffSection>

        <DiffSection title="Status changed" rows={diff.status_changed.length} accent="text-amber-300">
          {diff.status_changed.map((c) => (
            <div
              key={c.site}
              className="flex items-center justify-between border-b border-zinc-800 py-1 text-xs"
            >
              <span className="font-medium">{c.site}</span>
              <span className="text-zinc-400">
                {c.from ?? '∅'} → {c.to ?? '∅'}
              </span>
            </div>
          ))}
        </DiffSection>

        <DiffSection
          title="Confidence drift (≥15)"
          rows={diff.confidence_shift.length}
          accent="text-zinc-300"
        >
          {diff.confidence_shift.map((c) => (
            <div
              key={c.site}
              className="flex items-center justify-between border-b border-zinc-800 py-1 text-xs"
            >
              <span className="font-medium">{c.site}</span>
              <span className="text-zinc-400">
                {c.status} · {c.from_confidence} → {c.to_confidence}
              </span>
            </div>
          ))}
        </DiffSection>

        <p className="text-xs text-zinc-500">
          Unchanged: {diff.unchanged_count} sites
        </p>

        <div className="text-right">
          <button className="btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

function DiffSection({
  title,
  rows,
  accent,
  children,
}: {
  title: string
  rows: number
  accent: string
  children: React.ReactNode
}) {
  return (
    <div>
      <h4 className={`mb-2 text-sm font-semibold ${accent}`}>
        {title} <span className="text-xs text-zinc-500">({rows})</span>
      </h4>
      {rows === 0 ? (
        <p className="text-xs text-zinc-500">none</p>
      ) : (
        <div className="space-y-1">{children}</div>
      )}
    </div>
  )
}

function DiffRow({ site, url, status }: { site: string; url: string | null; status: Status }) {
  return (
    <div className="flex items-center justify-between border-b border-zinc-800 py-1 text-xs">
      <span className="font-medium">{site}</span>
      <span className="flex items-center gap-2 text-zinc-400">
        <span>{status}</span>
        {url && (
          <a href={url} target="_blank" rel="noopener noreferrer" className="link">
            open
          </a>
        )}
      </span>
    </div>
  )
}

function humanizeError(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return 'Something went wrong'
}
