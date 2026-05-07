import { useEffect, useMemo, useState } from 'react'
import { createMember, deleteMember, listMembers, patchMember } from '../api'
import { useAuth } from '../auth-context'
import { ApiError, type Member, type Role } from '../types'
import { HistoryView } from './HistoryView'
import { MlTab } from './MlTab'
import { SettingsTab } from './SettingsTab'
import { SitesEditor } from './SitesEditor'

type AdminTab = 'members' | 'sites' | 'history' | 'settings' | 'ml'

export function AdminPanel() {
  const [tab, setTab] = useState<AdminTab>('members')

  return (
    <section className="mx-auto mt-8 max-w-5xl space-y-6">
      <header>
        <h2 className="text-xl font-semibold tracking-tight">Admin</h2>
        <p className="mt-1 text-sm text-zinc-400">
          Manage members, the site catalog, scan history, and runtime knobs.
        </p>
      </header>

      <nav className="flex gap-1 border-b border-zinc-800 text-sm">
        <TabButton active={tab === 'members'} onClick={() => setTab('members')}>
          Members
        </TabButton>
        <TabButton active={tab === 'sites'} onClick={() => setTab('sites')}>
          Sites
        </TabButton>
        <TabButton active={tab === 'history'} onClick={() => setTab('history')}>
          History
        </TabButton>
        <TabButton active={tab === 'settings'} onClick={() => setTab('settings')}>
          Settings
        </TabButton>
        <TabButton active={tab === 'ml'} onClick={() => setTab('ml')}>
          ML
        </TabButton>
      </nav>

      {tab === 'members' && <MembersTab />}
      {tab === 'sites' && <SitesEditor />}
      {tab === 'history' && <HistoryView />}
      {tab === 'settings' && <SettingsTab />}
      {tab === 'ml' && <MlTab />}
    </section>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={
        'rounded-t px-3 py-2 transition ' +
        (active
          ? 'border-b-2 border-accent text-accent'
          : 'text-zinc-400 hover:text-zinc-200')
      }
    >
      {children}
    </button>
  )
}

function MembersTab() {
  const { member: me } = useAuth()
  const [members, setMembers] = useState<Member[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // create form
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState<Role>('member')
  const [creating, setCreating] = useState(false)

  // per-row reset state
  const [resetFor, setResetFor] = useState<string | null>(null)
  const [resetPw, setResetPw] = useState('')
  const [resetBusy, setResetBusy] = useState(false)

  useEffect(() => {
    void load()
  }, [])

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setMembers(await listMembers())
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setLoading(false)
    }
  }

  async function onCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError(null)
    try {
      await createMember({
        username: newUsername.trim(),
        password: newPassword,
        role: newRole,
      })
      setNewUsername('')
      setNewPassword('')
      setNewRole('member')
      await load()
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setCreating(false)
    }
  }

  async function onDelete(username: string) {
    if (!confirm(`Delete member "${username}"? This cannot be undone.`)) return
    setError(null)
    try {
      await deleteMember(username)
      await load()
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  async function onToggleRole(m: Member) {
    setError(null)
    const next: Role = m.role === 'admin' ? 'member' : 'admin'
    try {
      await patchMember(m.username, { role: next })
      await load()
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  async function onResetSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!resetFor) return
    setResetBusy(true)
    setError(null)
    try {
      await patchMember(resetFor, { password: resetPw })
      setResetFor(null)
      setResetPw('')
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setResetBusy(false)
    }
  }

  const sortedMembers = useMemo(
    () =>
      [...members].sort((a, b) => {
        if (a.role !== b.role) return a.role === 'admin' ? -1 : 1
        return a.username.localeCompare(b.username)
      }),
    [members],
  )

  return (
    <div className="space-y-4">
      <p className="text-xs text-zinc-400">
        Add, remove, and reset passwords for Reckora members. Members can search; only admins can
        manage other members.
      </p>

      {error && (
        <div className="card border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">{error}</div>
      )}

      <form
        onSubmit={onCreate}
        className="card grid gap-3 px-4 py-4 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-end"
      >
        <label className="block">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Username</span>
          <input
            required
            minLength={3}
            maxLength={32}
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            className="input mt-1 w-full"
            placeholder="e.g. ferdi"
          />
        </label>
        <label className="block">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Password</span>
          <input
            type="password"
            required
            minLength={6}
            maxLength={128}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            className="input mt-1 w-full"
            placeholder="min 6 chars"
          />
        </label>
        <label className="block">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Role</span>
          <select
            value={newRole}
            onChange={(e) => setNewRole(e.target.value as Role)}
            className="input mt-1 w-full"
          >
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <button type="submit" disabled={creating} className="btn-primary">
          {creating ? 'Adding…' : 'Add member'}
        </button>
      </form>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="border-b border-bg-border bg-zinc-900/40 text-left text-xs uppercase tracking-wider text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">Username</th>
              <th className="px-4 py-2 font-medium">Role</th>
              <th className="px-4 py-2 font-medium">Created</th>
              <th className="px-4 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={4} className="px-4 py-6 text-center text-sm text-zinc-500">
                  Loading members…
                </td>
              </tr>
            )}
            {!loading && sortedMembers.length === 0 && (
              <tr>
                <td colSpan={4} className="px-4 py-6 text-center text-sm text-zinc-500">
                  No members yet.
                </td>
              </tr>
            )}
            {sortedMembers.map((m) => {
              const isSelf = me?.username.toLowerCase() === m.username.toLowerCase()
              return (
                <tr key={m.username} className="border-b border-bg-border/60 last:border-b-0">
                  <td className="px-4 py-3 font-medium text-zinc-100">{m.username}</td>
                  <td className="px-4 py-3">
                    <span
                      className={
                        'rounded-full px-2 py-0.5 text-xs ' +
                        (m.role === 'admin'
                          ? 'bg-accent/20 text-accent'
                          : 'bg-zinc-800/80 text-zinc-300')
                      }
                    >
                      {m.role}
                    </span>
                    {isSelf && (
                      <span className="ml-2 text-xs text-zinc-500">(you)</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-zinc-400">
                    {new Date(m.created_at * 1000).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="inline-flex flex-wrap items-center justify-end gap-1.5">
                      <button
                        type="button"
                        className="chip hover:!text-white"
                        onClick={() => {
                          setResetFor(m.username)
                          setResetPw('')
                        }}
                      >
                        Reset password
                      </button>
                      <button
                        type="button"
                        className="chip hover:!text-white"
                        onClick={() => onToggleRole(m)}
                        disabled={isSelf && m.role === 'admin'}
                        title={
                          isSelf && m.role === 'admin'
                            ? 'You cannot demote yourself'
                            : `Switch role to ${m.role === 'admin' ? 'member' : 'admin'}`
                        }
                      >
                        {m.role === 'admin' ? 'Make member' : 'Make admin'}
                      </button>
                      <button
                        type="button"
                        className="chip !border-bad/40 !text-bad hover:!bg-bad/10"
                        onClick={() => onDelete(m.username)}
                        disabled={isSelf}
                        title={isSelf ? 'You cannot delete yourself' : 'Delete member'}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {resetFor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
          onClick={() => !resetBusy && setResetFor(null)}
        >
          <form
            onSubmit={onResetSubmit}
            onClick={(e) => e.stopPropagation()}
            className="card w-full max-w-sm space-y-4 px-5 py-5"
          >
            <h3 className="text-lg font-semibold tracking-tight">
              Reset password for <span className="text-accent">{resetFor}</span>
            </h3>
            <input
              type="password"
              autoFocus
              required
              minLength={6}
              maxLength={128}
              value={resetPw}
              onChange={(e) => setResetPw(e.target.value)}
              className="input w-full"
              placeholder="new password"
            />
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setResetFor(null)}
                disabled={resetBusy}
              >
                Cancel
              </button>
              <button type="submit" className="btn-primary" disabled={resetBusy}>
                {resetBusy ? 'Saving…' : 'Save'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}

function humanizeError(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return 'Something went wrong'
}
