/**
 * Phase 7d — admin-panel tab for the ML reranker retrain pipeline +
 * drift detector. Renders three blocks:
 *
 *   1. Top-line status: model age + last retrain timestamp +
 *      "Retrain now" button.
 *   2. Drift report: alarm summary + per-feature KS table.
 *   3. Last retrain log (collapsed by default).
 *
 * The tab is rendered inside ``AdminPanel``; auth is enforced by the
 * admin-only endpoints it calls.
 */

import { useEffect, useState } from 'react'
import {
  fetchMlStatus,
  triggerMlRetrain,
  type MlDriftReport,
  type MlFeatureDrift,
  type MlRetrainStatus,
  type MlStatusResponse,
} from '../api'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ok'; data: MlStatusResponse }
  | { kind: 'error'; message: string }

export function MlTab() {
  const [state, setState] = useState<LoadState>({ kind: 'loading' })
  const [retraining, setRetraining] = useState(false)
  const [showLog, setShowLog] = useState(false)

  // Initial fetch — runs once on mount. Doesn't synchronously call
  // setState; only the async resolution paths do, which dodges the
  // ``react-hooks/set-state-in-effect`` rule.
  useEffect(() => {
    let cancelled = false
    fetchMlStatus()
      .then((data) => {
        if (!cancelled) setState({ kind: 'ok', data })
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setState({
            kind: 'error',
            message: err instanceof Error ? err.message : String(err),
          })
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function refresh() {
    try {
      const data = await fetchMlStatus()
      setState({ kind: 'ok', data })
    } catch (err) {
      setState({
        kind: 'error',
        message: err instanceof Error ? err.message : String(err),
      })
    }
  }

  async function onRetrain() {
    setRetraining(true)
    try {
      await triggerMlRetrain()
      await refresh()
    } catch (err) {
      setState({
        kind: 'error',
        message: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setRetraining(false)
    }
  }

  if (state.kind === 'loading') {
    return (
      <div data-testid="ml-tab" className="text-sm text-zinc-400">
        Loading ML reranker status…
      </div>
    )
  }
  if (state.kind === 'error') {
    return (
      <div data-testid="ml-tab" className="card border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">
        {state.message}
      </div>
    )
  }

  const { data } = state
  return (
    <div data-testid="ml-tab" className="space-y-5">
      <header className="space-y-1">
        <p className="text-xs text-zinc-400">
          The Phase 4 reranker is a small ONNX logistic regression trained from the live
          site catalog. Phase 7d wires it into a one-click retrain + a Kolmogorov–Smirnov
          drift detector against the synthetic training distribution.
        </p>
      </header>

      <ModelStatusCard
        ageDays={data.model_age_days}
        lastRetrain={data.last_retrain}
        retraining={retraining}
        onRetrain={onRetrain}
      />

      <DriftCard drift={data.drift} />

      {data.last_retrain && (
        <section className="card px-4 py-3">
          <button
            type="button"
            onClick={() => setShowLog((v) => !v)}
            className="text-xs uppercase tracking-wider text-zinc-400 hover:text-zinc-200"
            data-testid="ml-toggle-log"
          >
            {showLog ? 'Hide' : 'Show'} last retrain log
          </button>
          {showLog && (
            <pre
              className="mt-2 max-h-64 overflow-auto rounded bg-bg-card/60 p-3 text-[11px] text-zinc-300"
              data-testid="ml-retrain-log"
            >
              {data.last_retrain.log || '(empty)'}
            </pre>
          )}
        </section>
      )}
    </div>
  )
}

function ModelStatusCard({
  ageDays,
  lastRetrain,
  retraining,
  onRetrain,
}: {
  ageDays: number | null
  lastRetrain: MlRetrainStatus | null
  retraining: boolean
  onRetrain: () => Promise<void>
}) {
  const ageLabel =
    ageDays === null ? 'no model file' : `${ageDays.toFixed(2)} days old`
  return (
    <section
      data-testid="ml-status-card"
      className="card flex flex-wrap items-center justify-between gap-3 px-4 py-3"
    >
      <div className="space-y-0.5">
        <div className="text-sm font-medium text-zinc-100">Model file</div>
        <div className="text-xs text-zinc-400" data-testid="ml-model-age">
          {ageLabel}
        </div>
        {lastRetrain && (
          <div className="text-xs text-zinc-500" data-testid="ml-last-retrain">
            last retrain:{' '}
            {new Date(lastRetrain.finished_at * 1000).toLocaleString()} ·{' '}
            {lastRetrain.ok ? 'ok' : 'failed'} · {lastRetrain.duration_s.toFixed(2)}s ·
            triggered by <code className="text-zinc-300">{lastRetrain.triggered_by || '—'}</code>
          </div>
        )}
        {lastRetrain && lastRetrain.error && (
          <div className="text-xs text-bad" data-testid="ml-last-error">
            error: {lastRetrain.error}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={() => void onRetrain()}
        disabled={retraining}
        className="chip border border-accent/40 bg-accent/10 text-xs text-accent hover:bg-accent/20 disabled:opacity-50"
        data-testid="ml-retrain-button"
      >
        {retraining ? 'Retraining…' : 'Retrain now'}
      </button>
    </section>
  )
}

function DriftCard({ drift }: { drift: MlDriftReport }) {
  return (
    <section data-testid="ml-drift-card" className="card px-4 py-3">
      <header className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-zinc-100">Drift report</h3>
        <span
          className={
            'chip border !py-0 text-[10px] ' +
            (drift.any_alarm
              ? 'border-warn/40 bg-warn/10 text-warn'
              : 'border-ok/40 bg-ok/10 text-ok')
          }
          data-testid="ml-drift-summary"
        >
          {drift.any_alarm
            ? `${drift.alarm_features.length} feature(s) drifted`
            : 'no drift detected'}
        </span>
      </header>
      <p className="mt-1 text-xs text-zinc-500">
        Two-sample Kolmogorov–Smirnov test against the synthetic training distribution.
        Threshold: <code>{drift.threshold.toFixed(2)}</code>.
      </p>
      {drift.features.length === 0 ? (
        <p className="mt-3 text-xs text-zinc-400">
          No production observations recorded yet — run a scan with ML scoring enabled.
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-400">
                <th className="py-1 pr-3">Feature</th>
                <th className="py-1 pr-3">KS</th>
                <th className="py-1 pr-3">p</th>
                <th className="py-1 pr-3">N (ref / cur)</th>
                <th className="py-1 pr-3">Alarm</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {drift.features.map((f) => (
                <DriftRow key={f.feature} f={f} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function DriftRow({ f }: { f: MlFeatureDrift }) {
  return (
    <tr
      className="border-b border-zinc-900/60"
      data-testid={`ml-drift-row-${f.feature}`}
      data-alarm={f.alarm ? '1' : '0'}
    >
      <td className="py-1 pr-3 text-zinc-200">{f.feature}</td>
      <td className="py-1 pr-3 text-zinc-300">{f.ks_statistic.toFixed(3)}</td>
      <td className="py-1 pr-3 text-zinc-300">{f.p_value.toFixed(3)}</td>
      <td className="py-1 pr-3 text-zinc-500">
        {f.reference?.count ?? 0} / {f.current?.count ?? 0}
      </td>
      <td className="py-1 pr-3">
        {f.skipped ? (
          <span className="text-zinc-500">—</span>
        ) : f.alarm ? (
          <span className="text-warn">alarm</span>
        ) : (
          <span className="text-zinc-500">ok</span>
        )}
      </td>
    </tr>
  )
}
