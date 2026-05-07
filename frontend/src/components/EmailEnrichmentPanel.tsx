/**
 * Phase 7b — WHOIS / HIBP-style enrichment side panel.
 *
 * Mounted under the result list when ``mode === 'list'`` and the most
 * recent scan was ``identity_type=email``. Calls ``POST /api/enrich/email``
 * once per email, surfaces:
 *  - the email's domain WHOIS (registrar, dates, NS, status)
 *  - the breach list from HIBP (when an admin key is configured)
 *  - a soft "disabled" hint when the admin knob is off so operators
 *    know enrichment exists and where to flip it.
 *
 * Both halves of the response can be ``null`` independently — WHOIS
 * upstream may fail while HIBP succeeds, or vice versa. The panel
 * renders whichever half came back.
 */

import { useEffect, useState } from 'react'
import { ApiError } from '../types'
import type { Breach, EnrichmentResponse, WhoisRecord } from '../types'
import { enrichEmail } from '../api'

type State =
  | { kind: 'loading' }
  | { kind: 'ok'; data: EnrichmentResponse }
  | { kind: 'rate_limited' }
  | { kind: 'error'; message: string }

interface Props {
  email: string
}

export function EmailEnrichmentPanel({ email }: Props) {
  // Initial state is ``loading`` (rather than ``idle`` + setState in
  // effect) so React's ``react-hooks/set-state-in-effect`` lint rule
  // is satisfied. The effect only fires the resolve / error paths.
  const [state, setState] = useState<State>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    enrichEmail(email)
      .then((data) => {
        if (cancelled) return
        setState({ kind: 'ok', data })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 429) {
          setState({ kind: 'rate_limited' })
          return
        }
        const message = err instanceof Error ? err.message : 'enrichment failed'
        setState({ kind: 'error', message })
      })
    return () => {
      cancelled = true
    }
  }, [email])

  if (state.kind === 'loading') {
    return (
      <section data-testid="email-enrichment" className="card px-4 py-3 text-sm text-zinc-400">
        Loading enrichment for <span className="text-zinc-200">{email}</span>…
      </section>
    )
  }

  if (state.kind === 'rate_limited') {
    return (
      <section data-testid="email-enrichment" className="card border-warn/40 bg-warn/10 px-4 py-3 text-sm text-warn">
        Enrichment rate limit hit — wait a minute before retrying.
      </section>
    )
  }

  if (state.kind === 'error') {
    return (
      <section data-testid="email-enrichment" className="card border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">
        Enrichment failed: {state.message}
      </section>
    )
  }

  const { data } = state
  if (data.disabled) {
    return (
      <section data-testid="email-enrichment" className="card px-4 py-3 text-sm text-zinc-500">
        Email enrichment is disabled. An admin can flip{' '}
        <code className="text-zinc-300">enable_email_enrichment</code> on in Settings to surface
        WHOIS + breach data here.
      </section>
    )
  }
  if (!data.result) {
    return null
  }
  return (
    <section data-testid="email-enrichment" className="card overflow-hidden">
      <header className="flex flex-wrap items-baseline gap-2 border-b border-bg-border px-4 py-2">
        <span className="text-xs uppercase tracking-wider text-zinc-500">enrichment</span>
        <span className="text-sm text-zinc-300">{data.result.email}</span>
        {data.result.domain && (
          <span className="text-xs text-zinc-500">· {data.result.domain}</span>
        )}
      </header>
      <div className="grid grid-cols-1 gap-4 px-4 py-3 sm:grid-cols-2">
        <WhoisCard record={data.result.whois} domain={data.result.domain} />
        <BreachesCard
          breaches={data.result.breaches}
          disabled={data.result.breaches_disabled}
        />
      </div>
    </section>
  )
}

function WhoisCard({ record, domain }: { record: WhoisRecord | null; domain: string | null }) {
  if (record === null) {
    return (
      <div data-testid="enrichment-whois" className="space-y-1 text-sm">
        <div className="text-xs uppercase tracking-wider text-zinc-500">whois</div>
        <div className="text-zinc-400">
          No WHOIS data for <span className="text-zinc-300">{domain ?? '?'}</span> — RDAP upstream
          didn't respond.
        </div>
      </div>
    )
  }
  return (
    <div data-testid="enrichment-whois" className="space-y-1 text-sm">
      <div className="text-xs uppercase tracking-wider text-zinc-500">whois</div>
      <Row label="domain" value={record.domain || domain || '—'} />
      <Row label="registrar" value={record.registrar ?? '—'} />
      <Row label="created" value={record.created ?? '—'} />
      <Row label="expires" value={record.expires ?? '—'} />
      {record.updated && <Row label="updated" value={record.updated} />}
      {record.nameservers.length > 0 && (
        <Row label="nameservers" value={record.nameservers.join(', ')} />
      )}
      {record.status.length > 0 && <Row label="status" value={record.status.join(', ')} />}
    </div>
  )
}

function BreachesCard({
  breaches,
  disabled,
}: {
  breaches: Breach[] | null
  disabled: boolean
}) {
  return (
    <div data-testid="enrichment-breaches" className="space-y-1 text-sm">
      <div className="flex items-baseline justify-between">
        <span className="text-xs uppercase tracking-wider text-zinc-500">breaches</span>
        {breaches !== null && (
          <span className="text-xs text-zinc-500">{breaches.length} found</span>
        )}
      </div>
      {disabled && (
        <div className="text-zinc-400">
          HIBP API key not configured — only WHOIS rendered.
        </div>
      )}
      {!disabled && breaches === null && (
        <div className="text-zinc-400">HIBP upstream didn't respond.</div>
      )}
      {breaches !== null && breaches.length === 0 && (
        <div className="text-good">No public breaches recorded for this address.</div>
      )}
      {breaches !== null && breaches.length > 0 && (
        <ul className="space-y-1.5">
          {breaches.map((b) => (
            <li key={b.name} className="rounded border border-bg-border/60 bg-bg-soft/50 px-2 py-1.5">
              <div className="flex flex-wrap items-baseline gap-1">
                <span className="font-medium text-zinc-200">{b.title}</span>
                {b.domain && <span className="text-xs text-zinc-500">· {b.domain}</span>}
                {b.breach_date && <span className="ml-auto text-xs text-zinc-500">{b.breach_date}</span>}
              </div>
              {b.data_classes.length > 0 && (
                <div className="mt-0.5 flex flex-wrap gap-1 text-[10px] uppercase tracking-wider text-zinc-500">
                  {b.data_classes.slice(0, 6).map((c) => (
                    <span key={c} className="rounded bg-bg-border/50 px-1 py-0.5">
                      {c}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <span className="w-24 shrink-0 text-xs uppercase tracking-wider text-zinc-500">{label}</span>
      <span className="text-zinc-200">{value}</span>
    </div>
  )
}
