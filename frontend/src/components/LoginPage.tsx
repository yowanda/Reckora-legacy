import { useState } from 'react'
import { useAuth } from '../auth-context'
import { ApiError } from '../types'

export function LoginPage() {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username.trim(), password)
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.status === 401 ? 'Invalid username or password.' : err.message)
      } else if (err instanceof Error) {
        setError(err.message)
      } else {
        setError('login failed')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen">
      <main className="mx-auto flex max-w-md flex-col items-center px-4 py-16 sm:py-24">
        <div className="mb-6 flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Logo className="h-8 w-8" />
          <span>cacadoxx</span>
        </div>
        <p className="mb-8 text-center text-sm text-zinc-400">
          Members-only username footprint scanner. Sign in to continue.
        </p>
        <form onSubmit={onSubmit} className="card w-full space-y-4 px-5 py-6">
          <label className="block">
            <span className="text-xs uppercase tracking-wider text-zinc-500">Username</span>
            <input
              autoFocus
              autoComplete="username"
              spellCheck={false}
              required
              minLength={3}
              maxLength={32}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="input mt-1 w-full"
            />
          </label>
          <label className="block">
            <span className="text-xs uppercase tracking-wider text-zinc-500">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              minLength={6}
              maxLength={128}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="input mt-1 w-full"
            />
          </label>
          {error && (
            <p className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
              {error}
            </p>
          )}
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <p className="mt-6 text-xs text-zinc-500">
          No self-signup. Ask your admin for an account.
        </p>
      </main>
    </div>
  )
}

function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true">
      <defs>
        <linearGradient id="logo-login" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#7c5cff" />
          <stop offset="100%" stopColor="#3ddc84" />
        </linearGradient>
      </defs>
      <circle cx="26" cy="26" r="14" fill="none" stroke="url(#logo-login)" strokeWidth="5" />
      <line x1="36" y1="36" x2="52" y2="52" stroke="url(#logo-login)" strokeWidth="6" strokeLinecap="round" />
    </svg>
  )
}
