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
          <span>cacadoxx</span>
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
            href="https://github.com/yowanda/Cacadoxx"
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
          <stop offset="0%" stopColor="#7c5cff" />
          <stop offset="100%" stopColor="#3ddc84" />
        </linearGradient>
      </defs>
      <circle cx="26" cy="26" r="14" fill="none" stroke="url(#logo-g)" strokeWidth="5" />
      <line x1="36" y1="36" x2="52" y2="52" stroke="url(#logo-g)" strokeWidth="6" strokeLinecap="round" />
    </svg>
  )
}
