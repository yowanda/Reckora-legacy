import { useEffect, useMemo, useRef, useState } from 'react'
import { Header } from './components/Header'
import { SearchBar } from './components/SearchBar'
import { TagFilter } from './components/TagFilter'
import { ResultCard } from './components/ResultCard'
import { Stats } from './components/Stats'
import { GraphView } from './components/GraphView'
import { Disclaimer } from './components/Disclaimer'
import { LoginPage } from './components/LoginPage'
import { AdminPanel } from './components/AdminPanel'
import { EmailEnrichmentPanel } from './components/EmailEnrichmentPanel'
import { fetchSites, streamGraphSearch, streamPermuteSearch, streamSearch } from './api'
import { useAuth } from './auth-context'
import type {
  CheckResult,
  GraphEdge,
  GraphExtracted,
  GraphProgress,
  IdentityType,
  ScanDepth,
  SearchProgress,
  SitesResponse,
} from './types'
import { resolveScanDepth } from './types'

type StatusFilter = 'all' | 'claimed' | 'free' | 'unknown' | 'blocked' | 'error'
type View = 'search' | 'admin'
type Mode = 'list' | 'graph' | 'permute'

interface PermuteState {
  base: string
  candidates: string[]
  current: string | null
  perCandidate: Record<string, { claimed: number; total: number; done: boolean }>
  filter: 'all' | string
}

const INITIAL_PERMUTE: PermuteState = {
  base: '',
  candidates: [],
  current: null,
  perCandidate: {},
  filter: 'all',
}

const INITIAL_GRAPH: GraphProgress = {
  username: '',
  state: 'idle',
  results: [],
  edges: [],
  phashEdges: [],
  extracted: [],
  secondaryScans: {},
  doneCount: 0,
}

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
  const [mode, setMode] = useState<Mode>('list')
  const [progress, setProgress] = useState<SearchProgress>({
    total: 0,
    done: 0,
    results: [],
    state: 'idle',
  })
  const [graphProgress, setGraphProgress] = useState<GraphProgress>(INITIAL_GRAPH)
  const [permuteProgress, setPermuteProgress] = useState<PermuteState>(INITIAL_PERMUTE)
  const [username, setUsername] = useState<string>('')
  // Phase 7b: keep the most recent ``identity_type`` so the SPA knows
  // when to mount the email enrichment side panel after a list-mode
  // scan finishes.
  const [currentIdentityType, setCurrentIdentityType] = useState<IdentityType>('username')
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    fetchSites()
      .then(setCatalog)
      .catch(() => setCatalog({ count: 0, tags: [], sites: [] }))
  }, [])

  function startSearch(
    name: string,
    identityType: IdentityType = 'username',
    scanDepth: ScanDepth = 250,
  ) {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setUsername(name)
    setCurrentIdentityType(identityType)
    // Phase 5g: resolve the picker preset to a concrete integer the
    // backend will accept. ``'all'`` -> live catalog count, clamped to
    // the 3,000-site backend hard cap.
    const limit = resolveScanDepth(scanDepth, catalog?.count)

    if (mode === 'permute') {
      setPermuteProgress({ ...INITIAL_PERMUTE, base: name })
      setProgress({ total: 0, done: 0, results: [], state: 'running' })
      streamPermuteSearch(
        { username: name, tags, limit, max_permutations: 10 },
        {
          signal: ctrl.signal,
          onCandidates: ({ candidates }) =>
            setPermuteProgress((p) => ({
              ...p,
              candidates,
              perCandidate: Object.fromEntries(
                [name, ...candidates].map((u) => [
                  u,
                  { claimed: 0, total: 0, done: false },
                ]),
              ),
            })),
          onStart: ({ total }) => setProgress((p) => ({ ...p, total, state: 'running' })),
          onCandidateStart: ({ username: candidate }) =>
            setPermuteProgress((p) => ({ ...p, current: candidate })),
          onResult: (r) => {
            setProgress((p) => ({
              ...p,
              done: p.done + 1,
              results: [...p.results, r],
            }))
            if (r.permutation_username && r.status === 'claimed') {
              setPermuteProgress((p) => {
                const stat = p.perCandidate[r.permutation_username!] ?? {
                  claimed: 0,
                  total: 0,
                  done: false,
                }
                return {
                  ...p,
                  perCandidate: {
                    ...p.perCandidate,
                    [r.permutation_username!]: { ...stat, claimed: stat.claimed + 1 },
                  },
                }
              })
            }
          },
          onCandidateDone: ({ username: candidate, claimed, total }) =>
            setPermuteProgress((p) => ({
              ...p,
              perCandidate: {
                ...p.perCandidate,
                [candidate]: { claimed, total, done: true },
              },
            })),
          onDone: () => {
            setProgress((p) => ({ ...p, state: 'done' }))
            setPermuteProgress((p) => ({ ...p, current: null }))
          },
          onInitialPdfReady: (e) =>
            setProgress((p) => ({ ...p, initialPdf: e })),
          onError: (msg) => setProgress((p) => ({ ...p, state: 'error', error: msg })),
        },
      )
      return
    }

    if (mode === 'graph') {
      setGraphProgress({ ...INITIAL_GRAPH, username: name, state: 'running' })
      setProgress({ total: 0, done: 0, results: [], state: 'running' })
      streamGraphSearch(
        { username: name, tags, limit, depth: 1 },
        {
          signal: ctrl.signal,
          onStart: ({ total }) =>
            setProgress((p) => ({ ...p, total, state: 'running' })),
          onResult: (r: CheckResult) => {
            setGraphProgress((g) => ({
              ...g,
              results: [...g.results, r],
              doneCount: g.doneCount + 1,
            }))
            // Surface primary results in the list view too so the user can
            // toggle between modes after the scan finishes.
            if (!r.parent_username) {
              setProgress((p) => ({
                ...p,
                done: p.done + 1,
                results: [...p.results, r],
              }))
            }
          },
          onExtracted: (e: GraphExtracted) =>
            setGraphProgress((g) => ({ ...g, extracted: [...g.extracted, e] })),
          onEdge: (e: GraphEdge) =>
            setGraphProgress((g) => ({ ...g, edges: [...g.edges, e] })),
          onPhashEdge: (e) =>
            setGraphProgress((g) => ({ ...g, phashEdges: [...g.phashEdges, e] })),
          onSecondaryStart: ({ child_username, total }) =>
            setGraphProgress((g) => ({
              ...g,
              secondaryScans: { ...g.secondaryScans, [child_username]: total },
            })),
          onDone: () => {
            setGraphProgress((g) => ({ ...g, state: 'done' }))
            setProgress((p) => ({ ...p, state: 'done' }))
          },
          onInitialPdfReady: (e) =>
            setProgress((p) => ({ ...p, initialPdf: e })),
          onError: (msg) => {
            setGraphProgress((g) => ({ ...g, state: 'error', error: msg }))
            setProgress((p) => ({ ...p, state: 'error', error: msg }))
          },
        },
      )
      return
    }

    setProgress({ total: 0, done: 0, results: [], state: 'running' })
    streamSearch(
      { username: name, tags, limit, identity_type: identityType },
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
        onInitialPdfReady: (e) =>
          setProgress((p) => ({ ...p, initialPdf: e })),
        onError: (msg) => setProgress((p) => ({ ...p, state: 'error', error: msg })),
      },
    )
  }

  function stopSearch() {
    abortRef.current?.abort()
    setProgress((p) => ({ ...p, state: 'done' }))
    setGraphProgress((g) => (g.state === 'running' ? { ...g, state: 'done' } : g))
  }

  const sortedResults = useMemo(() => {
    const order: Record<string, number> = {
      claimed: 0,
      blocked: 1,
      unknown: 2,
      error: 3,
      free: 4,
      skipped: 5,
    }
    return [...progress.results].sort((a, b) => {
      const oa = order[a.status] ?? 99
      const ob = order[b.status] ?? 99
      if (oa !== ob) return oa - ob
      // Within a status group, sort by descending confidence so the
      // strongest signals show first. Falls back to site name when ties.
      const ca = a.confidence ?? 0
      const cb = b.confidence ?? 0
      if (ca !== cb) return cb - ca
      return a.site.localeCompare(b.site)
    })
  }, [progress.results])

  const visibleResults = useMemo(() => {
    let out = sortedResults
    if (mode === 'permute' && permuteProgress.filter !== 'all') {
      out = out.filter((r) => {
        const handle = r.permutation_username ?? permuteProgress.base
        return handle === permuteProgress.filter
      })
    }
    if (statusFilter !== 'all') {
      out = out.filter((r) => r.status === statusFilter)
    }
    return out
  }, [sortedResults, statusFilter, mode, permuteProgress.filter, permuteProgress.base])

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
          // Phase 5a: graph + permute backends reject non-username
          // identity_type values, so lock the picker accordingly.
          lockedIdentityType={mode === 'list' ? undefined : 'username'}
          // Phase 5g: surface the live catalog count to the picker so
          // ``All sites`` shows the accurate "(~2,506)" hint.
          catalogCount={catalog?.count}
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

      <section className="mx-auto mt-3 flex max-w-3xl items-center gap-1.5">
        <span className="mr-1 text-xs uppercase tracking-wider text-zinc-500">view</span>
        {(['list', 'graph', 'permute'] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            disabled={isRunning && m !== mode}
            className={
              'chip transition ' +
              (mode === m
                ? '!border-accent !bg-accent/15 !text-white'
                : 'hover:!text-white disabled:opacity-50')
            }
            onClick={() => setMode(m)}
            title={
              m === 'graph'
                ? 'recursive identity-graph view (Phase 3)'
                : m === 'permute'
                  ? 'fan-out scan over common variants of the base handle (Phase 5c)'
                  : undefined
            }
          >
            {m}
          </button>
        ))}
        {mode === 'graph' && (
          <span className="ml-2 text-[10px] text-zinc-500">
            depth-1 recursion · scans up to {5} secondary usernames
          </span>
        )}
        {mode === 'permute' && (
          <span className="ml-2 text-[10px] text-zinc-500">
            base + up to 10 variants (separators · suffixes · prefixes · leetspeak)
          </span>
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

          {mode === 'graph' ? (
            <GraphView progress={graphProgress} />
          ) : (
            <>
              {mode === 'permute' && permuteProgress.candidates.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="mr-1 text-xs uppercase tracking-wider text-zinc-500">
                    handle
                  </span>
                  <button
                    type="button"
                    className={
                      'chip transition ' +
                      (permuteProgress.filter === 'all'
                        ? '!border-accent !bg-accent/15 !text-white'
                        : 'hover:!text-white')
                    }
                    onClick={() => setPermuteProgress((p) => ({ ...p, filter: 'all' }))}
                  >
                    all
                  </button>
                  {[permuteProgress.base, ...permuteProgress.candidates].map((u) => {
                    const stat = permuteProgress.perCandidate[u]
                    const claimed = stat?.claimed ?? 0
                    const isCurrent = permuteProgress.current === u
                    return (
                      <button
                        key={u}
                        type="button"
                        className={
                          'chip transition ' +
                          (permuteProgress.filter === u
                            ? '!border-accent !bg-accent/15 !text-white'
                            : isCurrent
                              ? 'hover:!text-white !border-accent/60'
                              : 'hover:!text-white')
                        }
                        onClick={() => setPermuteProgress((p) => ({ ...p, filter: u }))}
                      >
                        {u}
                        {claimed > 0 && (
                          <span className="ml-1 text-[10px] text-good">+{claimed}</span>
                        )}
                      </button>
                    )
                  })}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mr-1 text-xs uppercase tracking-wider text-zinc-500">show</span>
                {(['all', 'claimed', 'unknown', 'blocked', 'free', 'error'] as StatusFilter[]).map(
                  (s) => (
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
                  ),
                )}
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {visibleResults.map((r, i) => (
                  <ResultCard
                    key={`${r.site}-${r.permutation_username ?? r.parent_username ?? 'base'}-${i}`}
                    r={r}
                  />
                ))}
              </div>

              {visibleResults.length === 0 && progress.state === 'done' && (
                <p className="text-center text-sm text-zinc-500">
                  no results match the current filter.
                </p>
              )}
              {/* Phase 7b: WHOIS / HIBP-style enrichment panel for
                  email-mode scans. The component itself handles the
                  loading / disabled / error states; we just decide
                  when to mount it. */}
              {mode === 'list' &&
                currentIdentityType === 'email' &&
                progress.state === 'done' &&
                username && <EmailEnrichmentPanel email={username} />}
            </>
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
