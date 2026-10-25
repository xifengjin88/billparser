import { useEffect, useRef, useState } from 'react'

import type { CiteOut } from '@/api/client'
import { useSourceInfo, useSourceLines, useSourceTable } from '@/api/queries'
import { Empty, ErrorBox, Loading } from '@/components/app/common'
import { cn } from '@/lib/utils'

/** What the parser made of a line or row, as in the PDF coloring (invoiceparse/highlight.py). */
export const ROLE_CLASS: Record<string, string> = {
  flagged: 'bg-red-100/80 dark:bg-red-950/70',
  gap: 'bg-purple-100/80 dark:bg-purple-950/70',
  total: 'bg-orange-100/80 dark:bg-orange-950/70',
  charge: 'bg-blue-50 dark:bg-blue-950/60',
  not_counted: 'bg-slate-100 dark:bg-slate-800/70',
  section: 'bg-amber-50 dark:bg-amber-950/60',
  fact: 'bg-teal-50 dark:bg-teal-950/60',
}

export const LEGEND: [string, string][] = [
  ['charge', 'Charge'], ['total', 'Printed total'], ['section', 'Section heading'], ['fact', 'Fact'],
  ['not_counted', 'Not counted'], ['gap', 'Gap'], ['flagged', 'Flagged'],
]

export function Legend() {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
      {LEGEND.map(([role, text]) => (
        <span key={role} className="inline-flex items-center gap-1.5">
          <span className={cn('size-3 rounded-sm ring-1 ring-border', ROLE_CLASS[role])} /> {text}
        </span>
      ))}
    </div>
  )
}

export function pageImageUrl(sha: string, page: number, focusLine?: number | null, colored = true) {
  const p = new URLSearchParams({ page: String(page), colored: String(colored) })
  if (focusLine) p.set('focus_line', String(focusLine))
  return `/api/documents/${sha}/source/page.png?${p}`
}

/** Page image pixels per PDF point (billparse/highlight.py SCALE). */
const PAGE_SCALE = 1.6
/** A wide page is never shown smaller than this share of its drawn size, so its text stays readable. */
const MIN_ZOOM = 0.6

const ZOOM_STEP = 1.25

function PdfPage({ sha, page, line }: { sha: string; page: number; line?: number | null }) {
  const [loaded, setLoaded] = useState(false)
  const [natural, setNatural] = useState(0)             // the drawn page's width in pixels
  const [boxWidth, setBoxWidth] = useState(0)
  const [zoom, setZoom] = useState<number | null>(null) // share of the drawn size; null: fit (at least MIN_ZOOM)
  const box = useRef<HTMLDivElement>(null)
  const lines = useSourceLines(sha, page)
  const top = line ? lines.data?.find((l) => l.page === page && l.line === line)?.top : undefined
  const fit = natural ? Math.max(MIN_ZOOM, boxWidth / natural) : 1
  const shown = zoom ?? fit
  const width = natural ? Math.round(natural * shown) : undefined
  const setShown = (z: number) => setZoom(Math.min(4, Math.max(0.2, z)))

  // the clicked line in view (a third down the box) whenever the page or its size changes
  useEffect(() => {
    if (!box.current || !natural || top === null || top === undefined) return
    box.current.scrollTop = Math.max(0, top * PAGE_SCALE * shown - box.current.clientHeight / 3)
  }, [natural, shown, top])

  // ctrl/cmd + wheel (and a trackpad pinch) zooms the page instead of the browser
  useEffect(() => {
    const el = box.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      setZoom((z) => Math.min(4, Math.max(0.2, (z ?? fit) * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP))))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [fit])

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1 text-xs">
        <button type="button" className="h-6 w-6 rounded border hover:bg-muted" aria-label="Zoom out" onClick={() => setShown(shown / ZOOM_STEP)}>−</button>
        <span className="w-12 text-center tabular-nums text-muted-foreground">{Math.round(shown * 100)}%</span>
        <button type="button" className="h-6 w-6 rounded border hover:bg-muted" aria-label="Zoom in" onClick={() => setShown(shown * ZOOM_STEP)}>+</button>
        <button type="button" className="ml-1 h-6 rounded border px-2 hover:bg-muted" onClick={() => setZoom(null)}>Fit</button>
        <span className="ml-2 text-muted-foreground">Ctrl/⌘ + scroll to zoom</span>
      </div>
      <div
        ref={(el) => { box.current = el; if (el && el.clientWidth !== boxWidth) setBoxWidth(el.clientWidth) }}
        className="relative overflow-auto rounded-lg border bg-white"
        style={{ maxHeight: 640 }}
      >
        {!loaded && <div className="absolute inset-0"><Loading what={`Drawing page ${page}`} /></div>}
        <img
          key={`${page}-${line}`}
          src={pageImageUrl(sha, page, line)}
          alt={`Page ${page} of the PDF, each line colored by what the parser read and numbered`}
          className="max-w-none"
          style={{ width: width ?? '100%' }}
          onLoad={(e) => { setLoaded(true); setNatural(e.currentTarget.naturalWidth) }}
        />
      </div>
    </div>
  )
}

function TableAround({ sha, cite }: { sha: string; cite: CiteOut }) {
  const info = useSourceInfo(sha)
  const t = info.data?.tables.find((x) => x.name === cite.table)
  const r0 = cite.row !== null && cite.row !== undefined && t ? cite.row - t.row_offset : 0
  const offset = Math.max(0, r0 - 8)
  const q = useSourceTable(sha, t ? t.name : null, offset, 17)
  if (info.isLoading || q.isLoading) return <Loading what="Reading the source" />
  if (q.error) return <ErrorBox error={q.error} />
  if (!q.data) return <Empty>This table is not in the stored source.</Empty>
  return <SourceRows table={q.data} focusRow={cite.row ?? null} />
}

export function SourceRows({ table, focusRow }: { table: NonNullable<ReturnType<typeof useSourceTable>['data']>; focusRow?: number | null }) {
  const focusRef = useRef<HTMLTableRowElement>(null)
  useEffect(() => { focusRef.current?.scrollIntoView({ block: 'center' }) }, [focusRow, table])   // no return value: React would call it on unmount
  return (
    <div className="max-h-[520px] overflow-auto rounded-lg border">
      <table className="w-max min-w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-muted text-muted-foreground">
          <tr>
            <th className="border-r px-2 py-1 text-right font-medium">#</th>
            {table.columns.map((c) => <th key={c} className="px-2 py-1 text-left font-medium">{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r) => (
            <tr
              key={r.row}
              ref={r.row === focusRow ? focusRef : undefined}
              className={cn('border-t', ROLE_CLASS[r.role], r.row === focusRow && 'outline-2 -outline-offset-2 outline-yellow-500 bg-yellow-100 dark:bg-yellow-900/60')}
            >
              <td className="border-r px-2 py-1 text-right text-muted-foreground tabular-nums">{r.row}</td>
              {r.cells.map((v, i) => <td key={i} className="max-w-[320px] truncate px-2 py-1" title={v}>{v}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function LinesAround({ sha, cite }: { sha: string; cite: CiteOut }) {
  const q = useSourceLines(sha, cite.page ?? 1)
  const focusRef = useRef<HTMLDivElement>(null)
  useEffect(() => { focusRef.current?.scrollIntoView({ block: 'center' }) }, [q.data, cite.line])
  if (q.isLoading) return <Loading what="Reading the source" />
  if (q.error) return <ErrorBox error={q.error} />
  return (
    <div className="max-h-[520px] overflow-auto rounded-lg border font-mono text-xs">
      {(q.data ?? []).map((l) => (
        <div
          key={l.line}
          ref={l.line === cite.line ? focusRef : undefined}
          className={cn('flex gap-3 px-2 py-0.5 whitespace-pre', ROLE_CLASS[l.role], l.line === cite.line && 'bg-yellow-100 dark:bg-yellow-900/60')}
        >
          <span className="w-10 shrink-0 text-right text-muted-foreground">{l.line}</span>
          <span>{l.text}</span>
        </div>
      ))}
    </div>
  )
}

/** Where a value came from: the PDF page with its line, or the rows or lines around it. */
export function SourceView({ sha, cite }: { sha: string; cite: CiteOut | null | undefined }) {
  const info = useSourceInfo(sha)
  if (!cite) return <Empty>No location for this value.</Empty>
  if (info.isLoading) return <Loading what="Reading the source" />
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">Source: {cite.label}</div>
      {info.data?.has_pdf && cite.page ? (
        <PdfPage sha={sha} page={cite.page} line={cite.line} />
      ) : cite.table ? (
        <TableAround sha={sha} cite={cite} />
      ) : cite.line ? (
        <LinesAround sha={sha} cite={cite} />
      ) : null}
      {cite.text && <pre className="rounded-md bg-muted px-2 py-1.5 font-mono text-xs whitespace-pre-wrap break-words">{cite.text}</pre>}
    </div>
  )
}
