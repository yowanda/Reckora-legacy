import type { TagSummary } from '../types'

interface Props {
  tags: TagSummary[]
  selected: string[]
  onToggle: (tag: string) => void
  onClear: () => void
}

const PRIMARY_TAGS = ['social', 'coding', 'gaming', 'photo', 'music', 'video', 'blog', 'forum', 'shopping', 'finance', 'news']

export function TagFilter({ tags, selected, onToggle, onClear }: Props) {
  const byName = Object.fromEntries(tags.map((t) => [t.name, t.count]))
  const visible = PRIMARY_TAGS.filter((t) => byName[t]).map((name) => ({ name, count: byName[name] }))

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-xs uppercase tracking-wider text-zinc-500">categories</span>
      {visible.map((t) => {
        const active = selected.includes(t.name)
        return (
          <button
            key={t.name}
            type="button"
            onClick={() => onToggle(t.name)}
            className={
              'chip transition ' +
              (active
                ? '!border-accent !bg-accent/15 !text-white'
                : 'hover:!border-accent/50 hover:!text-white')
            }
          >
            {t.name} <span className="text-zinc-500">·{t.count}</span>
          </button>
        )
      })}
      {selected.length > 0 && (
        <button type="button" className="chip text-bad hover:!text-white" onClick={onClear}>
          clear
        </button>
      )}
    </div>
  )
}
