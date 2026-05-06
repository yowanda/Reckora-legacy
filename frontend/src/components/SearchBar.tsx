import { type FormEvent, useEffect, useRef, useState } from 'react'

interface Props {
  onSubmit: (username: string) => void
  isRunning: boolean
  onStop: () => void
  initial?: string
}

export function SearchBar({ onSubmit, isRunning, onStop, initial = '' }: Props) {
  const [value, setValue] = useState(initial)
  const inputRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  function handle(e: FormEvent) {
    e.preventDefault()
    const trimmed = value.trim()
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(trimmed)) {
      setError('only letters, digits, _ . - (max 64 chars)')
      return
    }
    setError(null)
    onSubmit(trimmed)
  }

  return (
    <form onSubmit={handle} className="space-y-2">
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          ref={inputRef}
          className="input flex-1"
          placeholder="enter a username, e.g. github, torvalds, jack"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={isRunning}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
        />
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
