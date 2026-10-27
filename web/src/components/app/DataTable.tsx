import { useEffect, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { ArrowDownIcon, ArrowUpIcon } from 'lucide-react'

import type { ColumnFilters, FilterKind } from '@/components/app/tableFilters'
import { cn } from '@/lib/utils'

export type Column<T> = {
  key: string
  header: React.ReactNode
  /** CSS grid track, e.g. "120px" or "minmax(200px, 2fr)". */
  width?: string
  align?: 'left' | 'right' | 'center'
  render: (row: T, index: number) => React.ReactNode
  className?: string
  /** The cell's value for filtering and sorting (with `filters` on the table). */
  value?: (row: T) => string | number | null | undefined
  /** The filter's kind; else told from the values (numbers, a few distinct values, or text). false: none. */
  filter?: FilterKind | false
}

/**
 * A table that renders only the rows in view, so 50,000 charges scroll smoothly. Columns are a CSS grid; the
 * table scrolls inside its own frame (the page never scrolls sideways).
 */
export function DataTable<T>({
  rows, columns, rowKey, onRowClick, selected, height = 520, rowHeight = 34, rowClassName, empty = 'No rows.', filters,
}: {
  rows: T[]
  columns: Column<T>[]
  rowKey: (row: T, index: number) => string
  onRowClick?: (row: T) => void
  selected?: string | null
  height?: number | 'auto'
  rowHeight?: number
  rowClassName?: (row: T) => string | undefined
  empty?: React.ReactNode
  /** Column filters and sort (useColumnFilters): a filter row under the header, and headers that sort on click. */
  filters?: ColumnFilters<T>
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  })
  const template = columns.map((c) => c.width ?? 'minmax(120px, 1fr)').join(' ')
  const auto = height === 'auto'
  const frameHeight = auto ? (rows.length ? Math.min(rows.length * rowHeight + 38, 640) : 110) : height
  const sortKey = filters?.sort?.replace(/^-/, '')
  const desc = filters?.sort?.startsWith('-')

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div ref={scrollRef} className="overflow-auto" style={{ height: frameHeight }}>
        <div className="min-w-max">
          <div
            className="sticky top-0 z-10 grid border-b bg-muted/70 text-xs font-medium text-muted-foreground backdrop-blur"
            style={{ gridTemplateColumns: template }}
          >
            {columns.map((c) => (
              <div key={c.key} className={cn('truncate px-2.5 py-2', c.align === 'right' && 'text-right', c.align === 'center' && 'text-center')}>
                {filters && c.value ? (
                  <button type="button" title="Sort" onClick={() => filters.toggleSort(c.key)}
                    className={cn('inline-flex max-w-full items-center gap-1 truncate hover:text-foreground', sortKey === c.key && 'text-foreground')}>
                    <span className="truncate">{c.header}</span>
                    {sortKey === c.key && (desc ? <ArrowDownIcon className="size-3 shrink-0" /> : <ArrowUpIcon className="size-3 shrink-0" />)}
                  </button>
                ) : c.header}
              </div>
            ))}
            {filters && columns.map((c) => (
              <div key={`f-${c.key}`} className="px-1.5 pb-1.5">
                {c.value && filters.meta[c.key] ? <FilterCell kind={filters.meta[c.key].kind} options={filters.meta[c.key].options}
                  value={filters.values[c.key] ?? ''} onChange={(v) => filters.set(c.key, v)} /> : null}
              </div>
            ))}
          </div>
          {rows.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">{empty}</div>
          ) : (
            <div className="relative" style={{ height: virtual.getTotalSize() }}>
              {virtual.getVirtualItems().map((v) => {
                const row = rows[v.index]
                const key = rowKey(row, v.index)
                return (
                  <div
                    key={key}
                    role={onRowClick ? 'button' : undefined}
                    tabIndex={onRowClick ? 0 : undefined}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    onKeyDown={onRowClick ? (e) => { if (e.key === 'Enter') onRowClick(row) } : undefined}
                    className={cn(
                      'absolute inset-x-0 grid items-center border-b text-sm last:border-b-0',
                      onRowClick && 'cursor-pointer hover:bg-muted/50 focus-visible:bg-muted focus-visible:outline-none',
                      selected === key && 'bg-sky-50 dark:bg-sky-950/60',
                      rowClassName?.(row),
                    )}
                    style={{ gridTemplateColumns: template, height: rowHeight, transform: `translateY(${v.start}px)` }}
                  >
                    {columns.map((c) => (
                      <div
                        key={c.key}
                        className={cn('truncate px-2.5', c.align === 'right' && 'text-right tabular-nums', c.align === 'center' && 'text-center', c.className)}
                      >
                        {c.render(row, v.index)}
                      </div>
                    ))}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

const FIELD = 'h-6 w-full rounded border bg-background px-1.5 text-xs font-normal text-foreground outline-none focus:border-ring'

function FilterCell({ kind, options, value, onChange }: {
  kind: FilterKind
  options: string[]
  value: string
  onChange: (v: string) => void
}) {
  if (kind === 'select') {
    return (
      <select className={FIELD} value={value} onChange={(e) => onChange(e.target.value)} title="Filter">
        <option value="">All</option>
        <option value="(empty)">(empty)</option>
        {options.map((o) => <option key={o} value={o}>{o.replaceAll('_', ' ')}</option>)}
      </select>
    )
  }
  return <TextFilter kind={kind} value={value} onChange={onChange} />
}

/** A text or number filter: its own text while typing, sent 300 ms after the last key (the URL holds the filter). */
function TextFilter({ kind, value, onChange }: { kind: FilterKind; value: string; onChange: (v: string) => void }) {
  const [text, setText] = useState(value)
  const sent = useRef(value)
  useEffect(() => {                                     // the URL changed elsewhere (Clear filters, back)
    if (value !== sent.current) {
      sent.current = value
      setText(value)
    }
  }, [value])
  useEffect(() => {
    if (text === sent.current) return
    const t = setTimeout(() => { sent.current = text; onChange(text) }, 300)
    return () => clearTimeout(t)
  }, [text, onChange])
  return (
    <input className={FIELD} value={text} onChange={(e) => setText(e.target.value)}
      placeholder={kind === 'number' ? '>10, <5, 1..9' : 'contains'}
      title={kind === 'number' ? 'A number, >, >=, <, <= a number, or a range lo..hi' : 'Text the value contains'} />
  )
}
