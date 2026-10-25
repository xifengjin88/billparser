import { useRef } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'

import { cn } from '@/lib/utils'

export type Column<T> = {
  key: string
  header: React.ReactNode
  /** CSS grid track, e.g. "120px" or "minmax(200px, 2fr)". */
  width?: string
  align?: 'left' | 'right' | 'center'
  render: (row: T, index: number) => React.ReactNode
  className?: string
}

/**
 * A table that renders only the rows in view, so 50,000 charges scroll smoothly. Columns are a CSS grid; the
 * table scrolls inside its own frame (the page never scrolls sideways).
 */
export function DataTable<T>({
  rows, columns, rowKey, onRowClick, selected, height = 520, rowHeight = 34, rowClassName, empty = 'No rows.',
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
                {c.header}
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
