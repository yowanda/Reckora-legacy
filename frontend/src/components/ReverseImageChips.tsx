/**
 * Phase 7c — reverse-image lookup chip row.
 *
 * Mounted under any CLAIMED result whose extractor pass produced an
 * ``avatar_url``. Calls ``POST /api/reverse-image`` once on mount and
 * renders a small row of provider chips (Yandex / Google Lens /
 * TinEye) that deep-link into each upstream's public reverse-image
 * search page. No API key, no upstream HTTP call from the backend —
 * the operator's browser hits the upstream directly when they click.
 *
 * The chips are rendered inline with the existing extracted-fields
 * panel so they don't bloat the always-visible card header.
 */

import { useEffect, useState } from 'react'
import type { ReverseImageProvider } from '../types'
import { reverseImageLookup } from '../api'

interface Props {
  imageUrl: string
}

export function ReverseImageChips({ imageUrl }: Props) {
  const [providers, setProviders] = useState<ReverseImageProvider[] | null>(null)
  const [disabled, setDisabled] = useState(false)

  useEffect(() => {
    let cancelled = false
    reverseImageLookup(imageUrl)
      .then((res) => {
        if (cancelled) return
        setProviders(res.providers)
        setDisabled(res.disabled)
      })
      .catch(() => {
        if (cancelled) return
        // Soft-fail: the chip row simply doesn't render. The user
        // still sees the rest of the extracted-fields panel.
        setProviders([])
      })
    return () => {
      cancelled = true
    }
  }, [imageUrl])

  if (providers === null) {
    return (
      <div data-testid="reverse-image-chips" className="text-xs text-zinc-500">
        Loading reverse-image links…
      </div>
    )
  }
  if (disabled || providers.length === 0) {
    return null
  }
  return (
    <div data-testid="reverse-image-chips" className="flex flex-wrap items-center gap-2">
      <span className="text-[10px] uppercase tracking-wider text-zinc-500">
        reverse-image
      </span>
      {providers.map((p) => (
        <a
          key={p.slug}
          href={p.url}
          target="_blank"
          rel="noreferrer noopener"
          data-testid={`reverse-image-${p.slug}`}
          className="chip border border-accent/40 bg-accent/10 !py-0 text-[10px] text-accent hover:bg-accent/20"
          title={`Search ${p.label} for visually similar images`}
        >
          ↻ {p.label}
        </a>
      ))}
    </div>
  )
}
