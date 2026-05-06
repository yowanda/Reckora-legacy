import { useState } from 'react'

const KEY = 'cacadoxx.disclaimer.v1'

export function Disclaimer() {
  const [open, setOpen] = useState(() =>
    typeof window !== 'undefined' && !window.localStorage.getItem(KEY),
  )

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-4 sm:items-center">
      <div className="card w-full max-w-lg p-6 shadow-2xl">
        <h2 className="text-lg font-semibold">Use responsibly</h2>
        <div className="mt-3 space-y-2 text-sm leading-relaxed text-zinc-300">
          <p>
            Cacadoxx queries publicly reachable URLs to check whether a username has a profile on
            various sites. It does not bypass authentication, scrape private data, or attempt
            credential discovery.
          </p>
          <p>
            Intended for legitimate use cases: auditing your own digital footprint, security
            research, and journalism. Do not use it to harass, dox, or target individuals.
          </p>
          <p className="text-zinc-500">
            By continuing you agree to these terms. Source code is{' '}
            <a className="text-accent hover:underline" href="https://github.com/yowanda/Cacadoxx" target="_blank" rel="noreferrer">on GitHub</a>.
          </p>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            className="btn-primary"
            onClick={() => {
              localStorage.setItem(KEY, '1')
              setOpen(false)
            }}
          >
            I understand
          </button>
        </div>
      </div>
    </div>
  )
}
