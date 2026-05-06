import { useEffect, useMemo, useRef, useState } from 'react'
import { Header } from './components/Header'
import { SearchBar } from './components/SearchBar'
import { TagFilter } from './components/TagFilter'
import { ResultCard } from './components/ResultCard'
import { Stats } from './components/Stats'
import { Disclaimer } from './components/Disclaimer'
import { LoginPage } from './components/LoginPage'
import { AdminPanel } from './components/AdminPanel'
import { fetchSites, streamSearch } from './api'
import { useAuth } from './auth-context'
import type { CheckResult, SearchProgress, SitesResponse } from './types'

type StatusFilter = 'all' | 'claimed' | 'free' | 'unknown' | 'error'
type View = 'search' | 'admin'

export default function App() {
  const { ready, member } = useAuth()

  if (!ready) return <Splash />
  if (!member) return <LoginPage />

  return <AppShell />
}

function AppShell() {
  const { member, logout } = useAuth()
  const [view, setView] = useState<View>('search')

  // Compute the effective view inline; non-admins are kept on "search".
  const effectiveView: View = view === 'admin' && member?.role !== 'admin' ? 'search' : view

  return (
    <div className="min-h-screen">
      <Header view={effectiveView} onChangeView={setView} />
      <main className="mx-auto max-w-6xl px-4 py-8 sm:py-14">
        {effectiveView === 'admin' ? <AdminPanel /> : <SearchView />}
        <Footer onLogout={() => void logout()} />
      </main>
      <Disclaimer />
    </div>
  )
}

function SearchView() {
  const [catalog, setCatalog] = useState<SitesResponse | null>(null)
  const [tags, setTags] = useState<string[]>([])
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [progress, setProgress] = useState<SearchProgress>({
    total: 0,
    done: 0,
    results: [],
    state: 'idle',
  })
  const [username, setUsername] = useState<string>('')
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    fetchSites()
      .then(setCatalog)
      .catch(() => setCatalog({ count: 0, tags: [], sites: [] }))
  }, [])

  function startSearch(name: string) {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setUsername(name)
    setProgress({ total: 0, done: 0, results: [], state: 'running' })

    streamSearch(
      { username: name, tags, limit: 250 },
      {
        signal: ctrl.signal,
        onStart: ({ total }) => setProgress((p) => ({ ...p, total, state: 'running' })),
        onResult: (r: CheckResult) =>
          setProgress((p) => ({
            ...p,
            done: p.done + 1,
            results: [...p.results, r],
          })),
        onDone: ({ cached }) =>
          setProgress((p) => ({ ...p, state: 'done', cached: !!cached })),
        onError: (msg) => setProgress((p) => ({ ...p, state: 'error', error: msg })),
      },
    )
  }

  function stopSearch() {
    abortRef.current?.abort()
    setProgress((p) => ({ ...p, state: 'done' }))
  }

  const sortedResults = useMemo(() => {
    const order = { claimed: 0, unknown: 1, error: 2, free: 3, skipped: 4 } as const
    return [...progress.results].sort((a, b) => {
      const oa = order[a.status] ?? 99
      const ob = order[b.status] ?? 99
      if (oa !== ob) return oa - ob
      return a.site.localeCompare(b.site)
    })
  }, [progress.results])

  const visibleResults = useMemo(() => {
    if (statusFilter === 'all') return sortedResults
    return sortedResults.filter((r) => r.status === statusFilter)
  }, [sortedResults, statusFilter])

  const isRunning = progress.state === 'running'

  return (
    <>
      <section className="mx-auto max-w-3xl text-center">
        <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">
          Recon any{' '}
          <span className="bg-gradient-to-r from-accent via-emerald-300 to-ok bg-clip-text text-transparent">
            username
          </span>
          <br className="hidden sm:inline" /> across the open web.
        </h1>
        <p className="mx-auto mt-4 max-w-xl text-zinc-400">
          Concurrent OSINT lookup over{' '}
          <span className="text-zinc-200">{catalog?.count ?? '500+'}</span> public sites. Results
          stream in live.
        </p>
      </section>

      <section className="mx-auto mt-8 max-w-3xl space-y-3">
        <SearchBar
          onSubmit={startSearch}
          isRunning={isRunning}
          onStop={stopSearch}
          initial={username}
        />
        {catalog && (
          <TagFilter
            tags={catalog.tags}
            selected={tags}
            onToggle={(t) =>
              setTags((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]))
            }
            onClear={() => setTags([])}
          />
        )}
      </section>

      {progress.state !== 'idle' && (
        <section className="mx-auto mt-8 max-w-5xl space-y-4">
          <Stats progress={progress} />

          {progress.error && (
            <div className="card border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">
              {progress.error}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-xs uppercase tracking-wider text-zinc-500">show</span>
            {(['all', 'claimed', 'unknown', 'free', 'error'] as StatusFilter[]).map((s) => (
              <button
                key={s}
                type="button"
                className={
                  'chip transition ' +
                  (statusFilter === s
                    ? '!border-accent !bg-accent/15 !text-white'
                    : 'hover:!text-white')
                }
                onClick={() => setStatusFilter(s)}
              >
                {s}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {visibleResults.map((r) => (
              <ResultCard key={r.site} r={r} />
            ))}
          </div>

          {visibleResults.length === 0 && progress.state === 'done' && (
            <p className="text-center text-sm text-zinc-500">
              no results match the current filter.
            </p>
          )}
        </section>
      )}
    </>
  )
}

function Splash() {
  return (
    <div className="flex min-h-screen items-center justify-center text-sm text-zinc-500">
      Loading…
    </div>
  )
}

function Footer({ onLogout }: { onLogout(): void }) {
  return (
    <footer className="mt-20 border-t border-bg-border pt-6 text-center text-xs text-zinc-500">
      <p>
        reckora · credit:{' '}
        <span className="text-zinc-300">cacapaspas</span> · for legitimate self-research and
        security work. Site database derived from{' '}
        <a
          className="hover:text-accent"
          href="https://github.com/soxoj/maigret"
          target="_blank"
          rel="noreferrer"
        >
          maigret
        </a>{' '}
        (MIT). Be kind out there.
      </p>
      <p className="mt-2">
        <button type="button" onClick={onLogout} className="hover:text-accent">
          sign out
        </button>
      </p>
    </footer>
  )
}
