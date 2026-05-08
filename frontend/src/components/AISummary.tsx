/**
 * Phase 10g — AI investigation summary panel.
 *
 * Mounted under the per-scan timeline (the ``ScanModal`` exports row in
 * ``HistoryView.tsx``). The panel streams the markdown summary from
 * ``POST /api/history/{scan_id}/ai_summary`` and renders it
 * incrementally so the operator sees the response materialise in real
 * time.
 *
 * Status routing:
 *   * ``200``                       — happy path, accumulating chunks.
 *   * ``412 Precondition Failed``   — admin hasn't configured an API
 *                                     key in ``secrets.json`` yet. The
 *                                     panel renders a "configure first"
 *                                     hint instead of the generic
 *                                     "summary failed" alert.
 *   * ``503 Service Unavailable``   — master switch
 *                                     ``enable_ai_summary`` is off.
 *                                     Same handling as 412 — the panel
 *                                     surfaces the admin knob name so
 *                                     the operator knows what to flip.
 */

import { useCallback, useState } from 'react'
import { ApiError } from '../types'
import { streamAiSummary } from '../api'

type State =
  | { kind: 'idle' }
  | { kind: 'loading'; partial: string }
  | { kind: 'ok'; markdown: string }
  | { kind: 'disabled'; message: string }
  | { kind: 'unconfigured'; message: string }
  | { kind: 'error'; message: string }

interface Props {
  scanId: number
}

export function AISummary({ scanId }: Props) {
  const [state, setState] = useState<State>({ kind: 'idle' })

  const onRun = useCallback(() => {
    let partial = ''
    setState({ kind: 'loading', partial })
    streamAiSummary(scanId, (chunk) => {
      partial += chunk
      setState({ kind: 'loading', partial })
    })
      .then(() => {
        setState({ kind: 'ok', markdown: partial })
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          if (err.status === 412) {
            setState({
              kind: 'unconfigured',
              message:
                'No API key configured. An admin must PUT one at ' +
                '``/api/admin/secrets/ai_summary_api_key`` before the ' +
                'AI summary can run.',
            })
            return
          }
          if (err.status === 503) {
            setState({
              kind: 'disabled',
              message:
                'AI summary is disabled. Flip ``enable_ai_summary`` in ' +
                'the admin Settings tab to opt into this feature.',
            })
            return
          }
          setState({ kind: 'error', message: err.message })
          return
        }
        const message = err instanceof Error ? err.message : 'AI summary failed'
        setState({ kind: 'error', message })
      })
  }, [scanId])

  return (
    <section
      data-testid="ai-summary"
      className="card border-t border-bg-border bg-bg-soft/40 px-4 py-3"
    >
      <header className="flex flex-wrap items-baseline gap-2">
        <span className="text-xs uppercase tracking-wider text-zinc-500">
          AI investigation summary
        </span>
        <span className="text-[10px] text-zinc-600">phase 10g</span>
        <button
          type="button"
          className="btn-ghost ml-auto text-xs"
          onClick={onRun}
          disabled={state.kind === 'loading'}
          data-testid="ai-summary-run"
        >
          {state.kind === 'loading'
            ? 'streaming…'
            : state.kind === 'ok'
              ? 'regenerate'
              : 'generate'}
        </button>
      </header>

      {state.kind === 'idle' && (
        <p className="mt-2 text-xs text-zinc-500">
          Generate a markdown investigation summary using the configured chat-completion provider
          (OpenAI, Anthropic, OpenRouter, or a local Ollama). The prompt is built from the scan's
          CLAIMED hits, extractor fields, and graph edges — no secrets leak to the model beyond what
          this panel renders.
        </p>
      )}

      {state.kind === 'loading' && (
        <pre
          data-testid="ai-summary-stream"
          className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border border-bg-border bg-bg/40 px-3 py-2 text-xs text-zinc-200"
        >
          {state.partial || 'connecting to provider…'}
        </pre>
      )}

      {state.kind === 'ok' && (
        <pre
          data-testid="ai-summary-result"
          className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-bg-border bg-bg/40 px-3 py-2 text-xs text-zinc-200"
        >
          {state.markdown}
        </pre>
      )}

      {state.kind === 'disabled' && (
        <p
          data-testid="ai-summary-disabled"
          className="mt-2 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
          role="status"
        >
          {state.message}
        </p>
      )}

      {state.kind === 'unconfigured' && (
        <p
          data-testid="ai-summary-unconfigured"
          className="mt-2 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
          role="status"
        >
          {state.message}
        </p>
      )}

      {state.kind === 'error' && (
        <p
          data-testid="ai-summary-error"
          className="mt-2 rounded border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad"
          role="alert"
        >
          AI summary failed: {state.message}
        </p>
      )}
    </section>
  )
}
