import { useEffect, useState } from 'react'
import { adminGetKnobs, adminPatchKnobs } from '../api'
import { ApiError, type AdminKnob, type AdminKnobKey } from '../types'

const PRETTY: Record<AdminKnobKey, string> = {
  graph_max_depth: 'Graph max depth',
  graph_max_secondary: 'Secondary fanout',
  graph_max_seed_hits: 'Seed hits cap',
  extract_min_confidence: 'Extractor confidence floor',
  proxy_enabled: 'Outbound proxy',
}

function knobsToDraft(rows: AdminKnob[]): Record<AdminKnobKey, number> {
  return Object.fromEntries(rows.map((k) => [k.key, k.effective])) as Record<
    AdminKnobKey,
    number
  >
}

export function SettingsTab() {
  const [knobs, setKnobs] = useState<AdminKnob[] | null>(null)
  const [draft, setDraft] = useState<Record<AdminKnobKey, number>>(
    {} as Record<AdminKnobKey, number>,
  )
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [savedFlash, setSavedFlash] = useState(false)

  // Network read on mount. Kept inside useEffect (no separate ``load``
  // closure) so the react-hooks linter doesn't flag setState happening
  // synchronously in an effect body — the work is fully async.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await adminGetKnobs()
        if (cancelled) return
        setKnobs(r.knobs)
        setDraft(knobsToDraft(r.knobs))
        setError(null)
      } catch (e) {
        if (cancelled) return
        setError(humanizeError(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  function dirty(): { values: Partial<Record<AdminKnobKey, number>>; resetKeys: AdminKnobKey[] } {
    const values: Partial<Record<AdminKnobKey, number>> = {}
    const resetKeys: AdminKnobKey[] = []
    if (!knobs) return { values, resetKeys }
    for (const k of knobs) {
      const desired = draft[k.key]
      if (desired === k.effective) continue
      // If user sets value back to default, prefer reset (drops the row).
      if (desired === k.default && k.override !== null) {
        resetKeys.push(k.key)
      } else {
        values[k.key] = desired
      }
    }
    return { values, resetKeys }
  }

  async function onSave() {
    const { values, resetKeys } = dirty()
    if (Object.keys(values).length === 0 && resetKeys.length === 0) return
    setBusy(true)
    setError(null)
    setSavedFlash(false)
    try {
      const r = await adminPatchKnobs({
        ...values,
        ...(resetKeys.length ? { reset: resetKeys } : {}),
      })
      setKnobs(r.knobs)
      setDraft(
        Object.fromEntries(r.knobs.map((k) => [k.key, k.effective])) as Record<AdminKnobKey, number>,
      )
      setSavedFlash(true)
      setTimeout(() => setSavedFlash(false), 2000)
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setBusy(false)
    }
  }

  async function onResetAll() {
    if (!knobs) return
    const resetKeys = knobs.filter((k) => k.override !== null).map((k) => k.key)
    if (resetKeys.length === 0) return
    if (!confirm(`Reset ${resetKeys.length} override(s) to defaults?`)) return
    setBusy(true)
    setError(null)
    try {
      const r = await adminPatchKnobs({ reset: resetKeys })
      setKnobs(r.knobs)
      setDraft(
        Object.fromEntries(r.knobs.map((k) => [k.key, k.effective])) as Record<AdminKnobKey, number>,
      )
      setSavedFlash(true)
      setTimeout(() => setSavedFlash(false), 2000)
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setBusy(false)
    }
  }

  if (knobs === null) {
    return <div className="text-sm text-zinc-500">Loading runtime knobs…</div>
  }

  const { values: pendingValues, resetKeys: pendingResets } = dirty()
  const hasPending = Object.keys(pendingValues).length + pendingResets.length > 0

  return (
    <div className="space-y-4">
      <p className="text-xs text-zinc-400">
        Live overrides on graph recursion + extractor knobs. Defaults come from the
        <code className="mx-1 rounded bg-zinc-800 px-1 py-0.5 text-[11px]">RECKORA_*</code>
        environment variables; saving here writes a row to the
        <code className="mx-1 rounded bg-zinc-800 px-1 py-0.5 text-[11px]">admin_settings</code>
        table that takes precedence at runtime. Phase 5d.
      </p>

      {error && (
        <div className="card border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">{error}</div>
      )}
      {savedFlash && (
        <div className="card border-good/40 bg-good/10 px-4 py-3 text-sm text-good">
          Saved.
        </div>
      )}

      <div className="space-y-3">
        {knobs.map((k) => (
          <KnobRow
            key={k.key}
            knob={k}
            draft={draft[k.key]}
            onChange={(v) => setDraft((d) => ({ ...d, [k.key]: v }))}
          />
        ))}
      </div>

      <div className="flex items-center justify-between gap-3 pt-2">
        <button
          onClick={() => void onResetAll()}
          disabled={busy || knobs.every((k) => k.override === null)}
          className="btn-ghost text-sm disabled:cursor-not-allowed disabled:opacity-40"
        >
          Reset all to defaults
        </button>
        <button
          onClick={() => void onSave()}
          disabled={busy || !hasPending}
          className="btn-primary disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </div>
  )
}

function KnobRow({
  knob,
  draft,
  onChange,
}: {
  knob: AdminKnob
  draft: number
  onChange: (v: number) => void
}) {
  const dirtyVal = draft !== knob.effective
  const overridden = knob.override !== null
  return (
    <div className="card px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="font-medium">{PRETTY[knob.key]}</span>
          <code className="ml-2 text-[11px] text-zinc-500">{knob.key}</code>
        </div>
        <div className="text-xs text-zinc-400">
          default <span className="font-mono text-zinc-200">{knob.default}</span>
          {overridden && (
            <>
              {' · '}
              override{' '}
              <span className="font-mono text-accent">{knob.override}</span>
            </>
          )}
          {' · '}
          range{' '}
          <span className="font-mono text-zinc-300">
            {knob.minimum}–{knob.maximum}
          </span>
        </div>
      </div>
      <p className="mt-1 text-xs text-zinc-400">{knob.description}</p>
      <div className="mt-3 flex items-center gap-3">
        <input
          type="range"
          min={knob.minimum}
          max={knob.maximum}
          value={draft}
          onChange={(e) => onChange(Number(e.target.value))}
          className="flex-1"
        />
        <input
          type="number"
          min={knob.minimum}
          max={knob.maximum}
          value={draft}
          onChange={(e) => {
            const v = Number(e.target.value)
            if (Number.isFinite(v)) onChange(clamp(v, knob.minimum, knob.maximum))
          }}
          className={
            'w-20 rounded border bg-zinc-900 px-2 py-1 text-right text-sm tabular-nums ' +
            (dirtyVal ? 'border-accent' : 'border-zinc-700')
          }
        />
      </div>
    </div>
  )
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v))
}

function humanizeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return 'Only admins can edit runtime knobs.'
    if (e.status === 400) return e.message
    return `Server error (${e.status})`
  }
  if (e instanceof Error) return e.message
  return 'Unknown error'
}
