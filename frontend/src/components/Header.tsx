import { useAuth } from '../auth-context'

interface Props {
  view: 'search' | 'admin'
  onChangeView(v: 'search' | 'admin'): void
}

export function Header({ view, onChangeView }: Props) {
  const { member, logout } = useAuth()
  const isAdmin = member?.role === 'admin'

  return (
    <header className="sticky top-0 z-30 border-b border-bg-border bg-bg/80 backdrop-blur">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <button
          type="button"
          onClick={() => onChangeView('search')}
          className="flex items-center gap-2 text-lg font-semibold tracking-tight"
        >
          <Logo className="h-7 w-7" />
          <span>reckora</span>
          <span className="ml-1 rounded-full bg-accent/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-accent">
            beta
          </span>
        </button>
        <nav className="flex items-center gap-2 text-sm text-zinc-400">
          <button
            type="button"
            onClick={() => onChangeView('search')}
            className={
              'btn-ghost ' + (view === 'search' ? '!text-white' : '')
            }
          >
            Search
          </button>
          {isAdmin && (
            <button
              type="button"
              onClick={() => onChangeView('admin')}
              className={
                'btn-ghost ' + (view === 'admin' ? '!text-white' : '')
              }
            >
              Admin
            </button>
          )}
          <a
            href="https://github.com/yowanda/Reckora"
            target="_blank"
            rel="noreferrer"
            className="btn-ghost"
          >
            GitHub
          </a>
          {member && (
            <span className="hidden items-center gap-2 sm:flex">
              <span className="text-xs text-zinc-500">
                signed in as <span className="text-zinc-300">{member.username}</span>
              </span>
              <button type="button" onClick={() => void logout()} className="btn-ghost">
                Logout
              </button>
            </span>
          )}
          {member && (
            <button
              type="button"
              onClick={() => void logout()}
              className="btn-ghost sm:hidden"
              aria-label="Logout"
            >
              Logout
            </button>
          )}
        </nav>
      </div>
    </header>
  )
}

function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true">
      <defs>
        <linearGradient id="logo-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#22d3ee" />
          <stop offset="100%" stopColor="#3ddc84" />
        </linearGradient>
      </defs>
      <circle cx="32" cy="32" r="22" fill="none" stroke="url(#logo-g)" strokeWidth="3" />
      <circle cx="32" cy="32" r="12" fill="none" stroke="url(#logo-g)" strokeWidth="2.4" opacity="0.8" />
      <circle cx="32" cy="32" r="2.8" fill="url(#logo-g)" />
      <line x1="32" y1="4"  x2="32" y2="14" stroke="url(#logo-g)" strokeWidth="3" strokeLinecap="round" />
      <line x1="32" y1="50" x2="32" y2="60" stroke="url(#logo-g)" strokeWidth="3" strokeLinecap="round" />
      <line x1="4"  y1="32" x2="14" y2="32" stroke="url(#logo-g)" strokeWidth="3" strokeLinecap="round" />
      <line x1="50" y1="32" x2="60" y2="32" stroke="url(#logo-g)" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}
