import { type FormEvent, useEffect, useRef, useState } from 'react'
import {
  DEFAULT_SCAN_DEPTH,
  IDENTITY_TYPES,
  IDENTITY_TYPE_META,
  SCAN_DEPTH_META,
  SCAN_DEPTH_PRESETS,
  type IdentityType,
  type ScanDepth,
} from '../types'

interface Props {
  onSubmit: (value: string, identity_type: IdentityType, scan_depth: ScanDepth) => void
  isRunning: boolean
  onStop: () => void
  initial?: string
  // Phase 5a: when the parent forces a specific identity (e.g. graph
  // and permute modes are username-only), pass `lockedIdentityType` to
  // hide the picker and pin the input to that type.
  lockedIdentityType?: IdentityType
  // Phase 5g: live catalog count so the "All sites" preset shows the
  // accurate number ("All sites (~2,506)").
  catalogCount?: number
}

export function SearchBar({
  onSubmit,
  isRunning,
  onStop,
  initial = '',
  lockedIdentityType,
  catalogCount,
}: Props) {
  const [value, setValue] = useState(initial)
  // ``pickedType`` is the user's most recent selection; the active
  // identity type is the parent-supplied lock when present, otherwise
  // the picker's own value. Deriving instead of mirroring the lock
  // into local state avoids the React 19 "setState in effect" lint.
  const [pickedType, setPickedType] = useState<IdentityType>('username')
  const identityType: IdentityType = lockedIdentityType ?? pickedType
  const [scanDepth, setScanDepth] = useState<ScanDepth>(DEFAULT_SCAN_DEPTH)
  const inputRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const meta = IDENTITY_TYPE_META[identityType]

  function handle(e: FormEvent) {
    e.preventDefault()
    const trimmed = value.trim()
    const re = new RegExp(meta.pattern)
    if (!re.test(trimmed)) {
      setError(meta.hint)
      return
    }
    setError(null)
    onSubmit(trimmed, identityType, scanDepth)
  }

  return (
    <form onSubmit={handle} className="space-y-2">
      <div className="flex flex-col gap-2 sm:flex-row">
        {!lockedIdentityType && (
          <select
            className="input h-12 sm:w-44"
            value={pickedType}
            onChange={(e) => {
              setPickedType(e.target.value as IdentityType)
              setError(null)
            }}
            disabled={isRunning}
            aria-label="Identity type"
            title="Pick an identifier type — the search input adapts its placeholder + validator"
          >
            {IDENTITY_TYPES.map((t) => (
              <option key={t} value={t}>
                {IDENTITY_TYPE_META[t].label}
              </option>
            ))}
          </select>
        )}
        <input
          ref={inputRef}
          className="input flex-1"
          placeholder={meta.placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={isRunning}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          // Type stays "text" because some browsers refuse to submit
          // invalid email/tel inputs but our backend re-validates and
          // gives clearer error messages anyway.
          type="text"
          inputMode={identityType === 'phone' ? 'tel' : 'text'}
        />
        <select
          className="input h-12 sm:w-44"
          value={String(scanDepth)}
          onChange={(e) => {
            const raw = e.target.value
            setScanDepth(raw === 'all' ? 'all' : (Number(raw) as ScanDepth))
          }}
          disabled={isRunning}
          aria-label="Scan depth"
          title="Pick how many sites to probe — sorted by alexa rank, top-N. ``All sites`` covers the full catalogue."
        >
          {SCAN_DEPTH_PRESETS.map((d) => {
            const m = SCAN_DEPTH_META[String(d)]
            const allLabel =
              d === 'all'
                ? `All sites${catalogCount ? ` (~${catalogCount.toLocaleString()})` : ''}`
                : m.label
            return (
              <option key={String(d)} value={String(d)}>
                {allLabel} (~{m.estimated_seconds}s)
              </option>
            )
          })}
        </select>
        {isRunning ? (
          <button type="button" onClick={onStop} className="btn-ghost h-12 px-5">
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!value.trim()} className="btn-primary h-12 px-6">
            <SearchIcon />
            Recon
          </button>
        )}
      </div>
      {error && <p className="text-sm text-bad">{error}</p>}
      {!error && identityType !== 'username' && !isRunning && (
        <p className="text-xs text-zinc-500">{meta.hint}</p>
      )}
      {!error && scanDepth === 'all' && !isRunning && (
        <p className="text-xs text-zinc-500">
          Scanning the full catalogue takes several minutes — results stream in
          live so you can stop anytime.
        </p>
      )}
    </form>
  )
}

function SearchIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="7" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  )
}
