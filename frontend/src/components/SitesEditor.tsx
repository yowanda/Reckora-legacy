import { useEffect, useMemo, useState } from 'react'
import {
  adminListSites,
  adminRemoveOverride,
  adminUpsertSite,
} from '../api'
import { ApiError, type AdminSiteEntry, type SiteRaw } from '../types'

/**
 * Phase 4: live DB editor for the bundled site catalog.
 *
 * The list shows the merged runtime catalog (base + overrides) with a
 * provenance pill on each row. Editing is restricted to the safe subset
 * that maps cleanly to ``site_overrides.patch_json`` (url, urlMain,
 * presence/absence strings, tags, plus the disabled flag).
 */
export function SitesEditor() {
  const [entries, setEntries] = useState<AdminSiteEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [filterSource, setFilterSource] = useState<'all' | 'base' | 'override' | 'custom'>('all')
  const [editing, setEditing] = useState<AdminSiteEntry | null>(null)
  const [creating, setCreating] = useState(false)
  const [stats, setStats] = useState({ total: 0, custom: 0, overridden: 0, disabled: 0 })

  useEffect(() => {
    void load()
  }, [])

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const r = await adminListSites()
      setEntries(r.sites)
      setStats({
        total: r.total,
        custom: r.custom_count,
        overridden: r.overridden_count,
        disabled: r.disabled_count,
      })
    } catch (e) {
      setError(humanizeError(e))
    } finally {
      setLoading(false)
    }
  }

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return entries.filter((e) => {
      if (filterSource !== 'all' && e.source !== filterSource) return false
      if (!q) return true
      return (
        e.name.toLowerCase().includes(q) ||
        e.tags.some((t) => t.toLowerCase().includes(q)) ||
        e.raw.url?.toLowerCase().includes(q)
      )
    })
  }, [entries, filter, filterSource])

  async function onToggleDisabled(entry: AdminSiteEntry) {
    setError(null)
    try {
      if (entry.disabled) {
        // currently disabled — drop the override so it reverts to base
        if (entry.source === 'override' && entry.override?.operation === 'disable') {
          await adminRemoveOverride(entry.name)
        } else {
          await adminUpsertSite({
            name: entry.name,
            operation: 'upsert',
            patch: { disabled: false },
          })
        }
      } else {
        await adminUpsertSite({
          name: entry.name,
          operation: 'disable',
          patch: {},
        })
      }
      await load()
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  async function onDelete(entry: AdminSiteEntry) {
    if (
      !confirm(
        `Drop "${entry.name}" from the runtime catalog? This persists as a delete-override; ` +
          `you can revert by removing the override later.`,
      )
    )
      return
    setError(null)
    try {
      await adminUpsertSite({ name: entry.name, operation: 'delete', patch: {} })
      await load()
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  async function onRevertOverride(entry: AdminSiteEntry) {
    if (!confirm(`Drop the override for "${entry.name}" and revert to the bundled definition?`))
      return
    setError(null)
    try {
      await adminRemoveOverride(entry.name)
      await load()
    } catch (e) {
      setError(humanizeError(e))
    }
  }

  return (
    <section className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold tracking-tight">Site catalog</h3>
          <p className="mt-1 text-xs text-zinc-400">
            Runtime catalog ({stats.total} sites) ={' '}
            <span className="text-zinc-300">bundled sites.json</span> +{' '}
            <span className="text-accent">{stats.overridden} overrides</span> +{' '}
            <span className="text-accent">{stats.custom} custom</span>. Disabled:{' '}
            {stats.disabled}.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost text-xs" onClick={() => void load()}>
            Refresh
          </button>
          <button className="btn-primary text-xs" onClick={() => setCreating(true)}>
            + Add custom site
          </button>
        </div>
      </header>

      {error && (
        <div className="card border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">{error}</div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <input
          className="input w-full max-w-md"
          placeholder="filter by name, url, or tag…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <select
          className="input"
          value={filterSource}
          onChange={(e) => setFilterSource(e.target.value as typeof filterSource)}
        >
          <option value="all">all sources</option>
          <option value="base">base</option>
          <option value="override">overridden</option>
          <option value="custom">custom</option>
        </select>
      </div>

      {loading ? (
        <p className="text-sm text-zinc-400">Loading catalog…</p>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 text-left text-xs uppercase tracking-wider text-zinc-500">
              <tr>
                <th className="px-3 py-2">Site</th>
                <th className="px-3 py-2">URL</th>
                <th className="px-3 py-2">Tags</th>
                <th className="px-3 py-2">Source</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 250).map((e) => (
                <tr
                  key={e.name}
                  className={
                    'border-t border-zinc-800 ' + (e.disabled ? 'opacity-60' : '')
                  }
                >
                  <td className="px-3 py-2 font-medium">{e.name}</td>
                  <td className="max-w-[320px] truncate px-3 py-2 text-xs text-zinc-400">
                    {e.raw.url}
                  </td>
                  <td className="px-3 py-2 text-xs text-zinc-400">
                    {e.tags.slice(0, 3).join(', ')}
                  </td>
                  <td className="px-3 py-2">
                    <SourcePill source={e.source} disabled={e.disabled} />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      className="btn-ghost text-xs"
                      onClick={() => setEditing(e)}
                    >
                      Edit
                    </button>
                    <button
                      className="btn-ghost text-xs"
                      onClick={() => void onToggleDisabled(e)}
                    >
                      {e.disabled ? 'Enable' : 'Disable'}
                    </button>
                    {e.source !== 'base' && (
                      <button
                        className="btn-ghost text-xs"
                        onClick={() => void onRevertOverride(e)}
                      >
                        Revert
                      </button>
                    )}
                    <button
                      className="btn-ghost text-xs text-bad"
                      onClick={() => void onDelete(e)}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
              {filtered.length > 250 && (
                <tr>
                  <td colSpan={5} className="px-3 py-2 text-xs text-zinc-500">
                    showing first 250 of {filtered.length} matches; refine filter to see more
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {(editing || creating) && (
        <SiteEditModal
          entry={editing}
          isCreate={creating}
          onClose={() => {
            setEditing(null)
            setCreating(false)
          }}
          onSaved={async () => {
            setEditing(null)
            setCreating(false)
            await load()
          }}
          onError={(msg) => setError(msg)}
        />
      )}
    </section>
  )
}

function SourcePill({ source, disabled }: { source: AdminSiteEntry['source']; disabled: boolean }) {
  const cls =
    source === 'custom'
      ? 'bg-accent/20 text-accent'
      : source === 'override'
        ? 'bg-amber-500/20 text-amber-300'
        : 'bg-zinc-700/50 text-zinc-300'
  return (
    <span className={'inline-flex gap-1 rounded-full px-2 py-0.5 text-xs ' + cls}>
      {source}
      {disabled ? ' · off' : ''}
    </span>
  )
}

function SiteEditModal({
  entry,
  isCreate,
  onClose,
  onSaved,
  onError,
}: {
  entry: AdminSiteEntry | null
  isCreate: boolean
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}) {
  const [name, setName] = useState(entry?.name ?? '')
  const [url, setUrl] = useState(entry?.raw.url ?? '')
  const [urlMain, setUrlMain] = useState(entry?.raw.urlMain ?? '')
  const [checkType, setCheckType] = useState(entry?.raw.checkType ?? 'status_code')
  const [presenceStrs, setPresenceStrs] = useState((entry?.raw.presenseStrs ?? []).join('\n'))
  const [absenceStrs, setAbsenceStrs] = useState((entry?.raw.absenceStrs ?? []).join('\n'))
  const [tags, setTags] = useState((entry?.raw.tags ?? []).join(', '))
  const [busy, setBusy] = useState(false)

  async function onSave() {
    setBusy(true)
    try {
      const patch: Partial<SiteRaw> = {}
      if (url) patch.url = url
      if (urlMain) patch.urlMain = urlMain
      if (checkType) patch.checkType = checkType
      const presence = presenceStrs
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean)
      const absence = absenceStrs
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean)
      if (presence.length) patch.presenseStrs = presence
      if (absence.length) patch.absenceStrs = absence
      const tagList = tags
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      if (tagList.length) patch.tags = tagList
      await adminUpsertSite({
        name: name.trim(),
        operation: 'upsert',
        patch,
      })
      onSaved()
    } catch (e) {
      onError(humanizeError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/70 p-4"
      onClick={() => !busy && onClose()}
    >
      <div
        className="card w-full max-w-2xl space-y-3 px-5 py-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold tracking-tight">
          {isCreate ? 'Add custom site' : `Edit "${entry?.name}"`}
        </h3>

        <label className="block text-sm">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Name</span>
          <input
            className="input mt-1 w-full"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={!isCreate}
            placeholder="GitHub, Twitter, MyForum…"
          />
        </label>

        <label className="block text-sm">
          <span className="text-xs uppercase tracking-wider text-zinc-500">
            URL (must contain {'{username}'})
          </span>
          <input
            className="input mt-1 w-full font-mono text-xs"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/{username}"
          />
        </label>

        <label className="block text-sm">
          <span className="text-xs uppercase tracking-wider text-zinc-500">URL main</span>
          <input
            className="input mt-1 w-full font-mono text-xs"
            value={urlMain}
            onChange={(e) => setUrlMain(e.target.value)}
            placeholder="https://example.com/"
          />
        </label>

        <label className="block text-sm">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Check type</span>
          <select
            className="input mt-1 w-full"
            value={checkType}
            onChange={(e) => setCheckType(e.target.value)}
          >
            <option value="status_code">status_code</option>
            <option value="message">message (presence/absence text)</option>
            <option value="response_url">response_url</option>
          </select>
        </label>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="text-xs uppercase tracking-wider text-zinc-500">
              Presence strings (one per line)
            </span>
            <textarea
              className="input mt-1 w-full font-mono text-xs"
              rows={4}
              value={presenceStrs}
              onChange={(e) => setPresenceStrs(e.target.value)}
              placeholder="@username&#10;profile_id"
            />
          </label>
          <label className="block text-sm">
            <span className="text-xs uppercase tracking-wider text-zinc-500">
              Absence strings (one per line)
            </span>
            <textarea
              className="input mt-1 w-full font-mono text-xs"
              rows={4}
              value={absenceStrs}
              onChange={(e) => setAbsenceStrs(e.target.value)}
              placeholder="not found&#10;404"
            />
          </label>
        </div>

        <label className="block text-sm">
          <span className="text-xs uppercase tracking-wider text-zinc-500">
            Tags (comma-separated)
          </span>
          <input
            className="input mt-1 w-full"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="forum, gaming"
          />
        </label>

        <div className="flex items-center justify-end gap-2 pt-2">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn-primary" onClick={() => void onSave()} disabled={busy || !name}>
            {busy ? 'Saving…' : 'Save override'}
          </button>
        </div>
      </div>
    </div>
  )
}

function humanizeError(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return 'Something went wrong'
}
