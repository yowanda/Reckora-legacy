import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useMemo, useState } from 'react'
import type { CheckResult, GraphExtracted, GraphProgress } from '../types'

const ROOT_NODE = 'root'

type RootData = {
  kind: 'root'
  username: string
  primaryHits: number
}
type SiteData = {
  kind: 'site'
  result: CheckResult
}
type SecondaryData = {
  kind: 'secondary'
  username: string
  evidence: string
  source_site: string
  hits: number
}

type GraphNodeData = RootData | SiteData | SecondaryData

const nodeTypes = {
  root: RootNodeView,
  site: SiteNodeView,
  secondary: SecondaryNodeView,
}

interface GraphViewProps {
  progress: GraphProgress
  onSelect?: (sel: GraphExtracted | null) => void
}

export function GraphView({ progress, onSelect }: GraphViewProps) {
  const [selectedSite, setSelectedSite] = useState<string | null>(null)

  const { nodes, edges, sitePositions } = useMemo(
    () => layout(progress),
    [progress],
  )

  const selectedExtracted = useMemo<GraphExtracted | null>(() => {
    if (!selectedSite) return null
    return progress.extracted.find((e) => `${e.parent_username}:${e.site}` === selectedSite) ?? null
  }, [selectedSite, progress.extracted])

  const handleNodeClick = (_e: unknown, node: Node) => {
    if (node.type === 'site') {
      const data = node.data as SiteData
      const key = `${progress.username}:${data.result.site}`
      setSelectedSite((cur) => (cur === key ? null : key))
      const next = progress.extracted.find(
        (e) => e.parent_username === progress.username && e.site === data.result.site,
      )
      onSelect?.(next ?? null)
    } else {
      setSelectedSite(null)
      onSelect?.(null)
    }
  }

  if (progress.state === 'idle') return null

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
      <div className="card relative h-[520px] overflow-hidden p-0 sm:h-[640px]">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.4}
          maxZoom={1.5}
          panOnScroll
          panOnDrag
          nodesDraggable={false}
          onNodeClick={handleNodeClick}
          proOptions={{ hideAttribution: true }}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={24}
            size={1}
            className="!bg-bg !text-zinc-800"
          />
          <Controls
            showInteractive={false}
            className="!border-bg-border !bg-bg-card !text-zinc-300"
          />
        </ReactFlow>
        {progress.state === 'running' && (
          <div className="pointer-events-none absolute right-3 top-3 rounded-md border border-bg-border bg-bg-card/90 px-2 py-1 font-mono text-[10px] text-zinc-400">
            {progress.doneCount}/{sitePositions.size + Object.keys(progress.secondaryScans).length}{' '}
            scanning…
          </div>
        )}
      </div>
      <SidePanel progress={progress} extracted={selectedExtracted} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------

function layout(progress: GraphProgress) {
  const nodes: Node<GraphNodeData>[] = []
  const edges: Edge[] = []
  const sitePositions = new Map<string, { x: number; y: number }>()

  const claimedPrimary = progress.results.filter(
    (r) => !r.parent_username && r.status === 'claimed',
  )
  const sitesWithExtracts = new Set(
    progress.extracted
      .filter((e) => e.parent_username === progress.username)
      .map((e) => e.site),
  )
  const sitesWithEdges = new Set(progress.edges.map((e) => e.source_site))
  // Phase 7a: include sites referenced by pHash-derived edges so the
  // dashed purple stitches always have both endpoints rendered.
  const sitesWithPhashEdges = new Set<string>()
  progress.phashEdges.forEach((e) => {
    sitesWithPhashEdges.add(e.site_a)
    sitesWithPhashEdges.add(e.site_b)
  })
  const siteOrder = claimedPrimary
    .filter(
      (r) =>
        sitesWithExtracts.has(r.site) ||
        sitesWithEdges.has(r.site) ||
        sitesWithPhashEdges.has(r.site),
    )
    .slice(0, 12)
  const siteFallback = claimedPrimary
    .filter((r) => !siteOrder.includes(r))
    .slice(0, 12 - siteOrder.length)
  const sitesToRender = [...siteOrder, ...siteFallback]

  // Root node (left).
  nodes.push({
    id: ROOT_NODE,
    type: 'root',
    position: { x: 0, y: 0 },
    data: {
      kind: 'root',
      username: progress.username,
      primaryHits: claimedPrimary.length,
    },
  })

  // Site column (middle).
  const siteX = 320
  const siteSpacing = 96
  const siteStartY = -((sitesToRender.length - 1) * siteSpacing) / 2
  sitesToRender.forEach((r, idx) => {
    const id = `site:${r.site}`
    const y = siteStartY + idx * siteSpacing
    nodes.push({
      id,
      type: 'site',
      position: { x: siteX, y },
      data: { kind: 'site', result: r },
    })
    sitePositions.set(r.site, { x: siteX, y })
    edges.push({
      id: `e:${ROOT_NODE}->${id}`,
      source: ROOT_NODE,
      target: id,
      type: 'smoothstep',
      animated: false,
      style: { stroke: '#3f3f46', strokeWidth: 1.5 },
    })
  })

  // Phase 7a: stitch pHash-derived edges between two ``site:`` nodes
  // in the middle column. Skip pairs where one side isn't currently
  // rendered (the layout caps at 12 sites) so the React-Flow edge
  // doesn't dangle. Distinct purple stroke + dashed pattern + a
  // "↺ phash" label keeps them visually separate from the green
  // secondary edges below.
  const renderedSites = new Set(sitesToRender.map((r) => r.site))
  progress.phashEdges.forEach((edge) => {
    if (!renderedSites.has(edge.site_a) || !renderedSites.has(edge.site_b)) {
      return
    }
    edges.push({
      id: `phash:${edge.site_a}::${edge.site_b}`,
      source: `site:${edge.site_a}`,
      target: `site:${edge.site_b}`,
      label: `↺ phash · d=${edge.hamming}`,
      type: 'straight',
      animated: false,
      style: { stroke: '#a855f7', strokeWidth: 1.5, strokeDasharray: '6 4' },
      labelStyle: { fill: '#c4b5fd', fontSize: 10, fontFamily: 'ui-monospace, monospace' },
      labelBgStyle: { fill: '#0a0a0a', fillOpacity: 0.9 },
      data: { kind: 'phash' },
    })
  })

  // Secondary column (right).
  const secondaryX = 720
  const secondarySpacing = 96
  const childList = Array.from(
    new Map(
      progress.edges.map((e) => [e.child_username, e]),
    ).values(),
  )
  const secondaryStartY = -((childList.length - 1) * secondarySpacing) / 2
  childList.forEach((edge, idx) => {
    const childId = `child:${edge.child_username}`
    const y = secondaryStartY + idx * secondarySpacing
    const subHits = progress.results.filter(
      (r) => r.parent_username === edge.child_username && r.status === 'claimed',
    ).length
    nodes.push({
      id: childId,
      type: 'secondary',
      position: { x: secondaryX, y },
      data: {
        kind: 'secondary',
        username: edge.child_username,
        evidence: edge.evidence,
        source_site: edge.source_site,
        hits: subHits,
      },
    })
    edges.push({
      id: `edge:${edge.source_site}->${edge.child_username}`,
      source: `site:${edge.source_site}`,
      target: childId,
      label: edge.evidence,
      type: 'smoothstep',
      animated: progress.state === 'running',
      style: { stroke: '#34d399', strokeWidth: 1.5 },
      labelStyle: { fill: '#a1a1aa', fontSize: 10, fontFamily: 'ui-monospace, monospace' },
      labelBgStyle: { fill: '#0a0a0a', fillOpacity: 0.9 },
    })
  })

  return { nodes, edges, sitePositions }
}

// ---------------------------------------------------------------------------
// node renderers
// ---------------------------------------------------------------------------

function RootNodeView({ data }: NodeProps) {
  const d = data as RootData
  return (
    <div className="rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-zinc-100 shadow-sm">
      <div className="text-[10px] uppercase tracking-wider text-accent/80">root</div>
      <div className="font-mono text-sm">{d.username}</div>
      <div className="text-[10px] text-zinc-400">{d.primaryHits} CLAIMED</div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !bg-accent" />
    </div>
  )
}

function SiteNodeView({ data }: NodeProps) {
  const d = data as SiteData
  const r = d.result
  const claimed = r.status === 'claimed'
  return (
    <div
      className={
        'min-w-[140px] rounded-md border px-3 py-2 text-xs shadow-sm transition ' +
        (claimed
          ? 'border-ok/40 bg-ok/10 text-zinc-100 hover:border-ok'
          : 'border-zinc-700 bg-zinc-900/40 text-zinc-300')
      }
    >
      <div className="flex items-center gap-1.5">
        <span className={`h-1.5 w-1.5 rounded-full ${claimed ? 'bg-ok' : 'bg-zinc-500'}`} />
        <span className="truncate font-medium">{r.site}</span>
      </div>
      {typeof r.confidence === 'number' && (
        <div className="mt-1 font-mono text-[10px] text-zinc-500">
          conf {r.confidence}
          {r.verified === true && <span className="ml-1 text-ok">✓</span>}
          {r.extracted && Object.keys(r.extracted).length > 0 && (
            <span className="ml-1 text-accent">↗ ext</span>
          )}
        </div>
      )}
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !bg-zinc-600" />
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !bg-zinc-600" />
    </div>
  )
}

function SecondaryNodeView({ data }: NodeProps) {
  const d = data as SecondaryData
  return (
    <div className="min-w-[140px] rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-zinc-100 shadow-sm">
      <div className="text-[10px] uppercase tracking-wider text-emerald-300">secondary</div>
      <div className="font-mono text-sm">{d.username}</div>
      <div className="text-[10px] text-zinc-400">
        via <span className="font-mono">{d.evidence}</span> · {d.hits} hits
      </div>
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !bg-emerald-400" />
    </div>
  )
}

// ---------------------------------------------------------------------------
// side panel
// ---------------------------------------------------------------------------

function SidePanel({
  progress,
  extracted,
}: {
  progress: GraphProgress
  extracted: GraphExtracted | null
}) {
  if (extracted) {
    return (
      <div className="card overflow-y-auto p-4">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">
          extracted from {extracted.site}
        </div>
        <div className="mt-1 truncate text-sm text-zinc-300">
          <a
            href={extracted.url}
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-accent"
          >
            {extracted.url}
          </a>
        </div>
        <div className="mt-3 space-y-1.5">
          {Object.entries(extracted.fields).length === 0 && (
            <div className="text-xs italic text-zinc-500">no recognised fields</div>
          )}
          {Object.entries(extracted.fields).map(([k, v]) => (
            <div key={k} className="text-xs">
              <span className="font-mono text-zinc-500">{k}</span>{' '}
              <span className="break-all text-zinc-200">{v}</span>
            </div>
          ))}
        </div>
      </div>
    )
  }

  // Default: graph summary stats.
  const claimed = progress.results.filter((r) => r.status === 'claimed')
  const primaryClaimed = claimed.filter((r) => !r.parent_username)
  const secondaryClaimed = claimed.filter((r) => r.parent_username)
  const extractedCount = progress.extracted.length
  const recursionFanout = progress.edges.length

  return (
    <div className="card flex flex-col gap-3 p-4 text-sm">
      <div>
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">root</div>
        <div className="font-mono text-zinc-100">{progress.username}</div>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <Stat label="primary CLAIMED" value={primaryClaimed.length} />
        <Stat label="extracted" value={extractedCount} />
        <Stat label="recursion edges" value={recursionFanout} />
        <Stat label="secondary CLAIMED" value={secondaryClaimed.length} />
        <Stat label="pHash matches" value={progress.phashEdges.length} />
      </div>
      <p className="text-xs text-zinc-500">
        Click a CLAIMED site node to inspect the identifiers Reckora extracted from its page body.
        Green edges show secondary usernames Reckora discovered and rescanned; dashed purple edges
        connect CLAIMED hits whose profile-image perceptual hashes match closely.
      </p>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-bg-border bg-bg-card/40 p-2">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="font-mono text-base text-zinc-100">{value}</div>
    </div>
  )
}
