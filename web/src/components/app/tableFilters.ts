import { useMemo } from 'react'
import { useSearchParams } from 'react-router'

import type { Column } from '@/components/app/DataTable'

export type FilterKind = 'text' | 'select' | 'number'

/** At most this many distinct values: the column filters with a list, else with a text box. */
const MAX_OPTIONS = 30

type Cell = string | number | null | undefined

/**
 * A number filter: "100" (equal), ">100", ">=100", "<5", "<=5", "10..20" (both included). Unknown text: no filter.
 */
function numberTest(expr: string): ((v: number) => boolean) | null {
  const s = expr.replaceAll(',', '').trim()
  let m = s.match(/^(-?\d*\.?\d+)\s*\.\.\s*(-?\d*\.?\d+)$/)
  if (m) {
    const [lo, hi] = [Number(m[1]), Number(m[2])]
    return (v) => v >= lo && v <= hi
  }
  m = s.match(/^(>=|<=|>|<|=)?\s*(-?\d*\.?\d+)$/)
  if (!m) return null
  const x = Number(m[2])
  switch (m[1]) {
    case '>': return (v) => v > x
    case '>=': return (v) => v >= x
    case '<': return (v) => v < x
    case '<=': return (v) => v <= x
    default: return (v) => v === x
  }
}

/**
 * Filters and sort for a DataTable, one per column that has a `value`, kept in the URL (prefix + column key; sort in
 * prefix + "sort", "-key" for descending), so a filtered view survives a reload and can be shared.
 */
export function useColumnFilters<T>(rows: T[], columns: Column<T>[], prefix = 'f.') {
  const [params, setParams] = useSearchParams()
  const values = useMemo(() => {
    const out: Record<string, string> = {}
    for (const c of columns) {
      const v = params.get(prefix + c.key)
      if (v) out[c.key] = v
    }
    return out
  }, [params, columns, prefix])
  const sort = params.get(prefix + 'sort')

  // each column's kind: given, else numbers -> number, few distinct values -> select, else text
  const meta = useMemo(() => {
    const out: Record<string, { kind: FilterKind; options: string[] }> = {}
    for (const c of columns) {
      if (!c.value || c.filter === false) continue
      const seen = new Set<string>()
      let numbers = 0
      let filled = 0
      for (const r of rows) {
        const v = c.value(r)
        if (v === null || v === undefined || v === '') continue
        filled++
        if (typeof v === 'number') numbers++
        if (seen.size <= MAX_OPTIONS) seen.add(String(v))
      }
      if (!filled && !c.filter) continue                  // no value in any row: nothing to filter, kind unknown
      const kind: FilterKind = c.filter ?? (numbers === filled ? 'number' : seen.size <= MAX_OPTIONS ? 'select' : 'text')
      out[c.key] = { kind, options: kind === 'select' ? Array.from(seen).sort() : [] }
    }
    return out
  }, [rows, columns])

  const shown = useMemo(() => {
    const tests = columns
      .filter((c) => c.value && values[c.key] && meta[c.key])
      .map((c) => {
        const want = values[c.key]
        const get = c.value as (row: T) => Cell
        const kind = meta[c.key].kind
        if (kind === 'number') {
          const t = numberTest(want)
          return t ? (r: T) => { const v = get(r); return typeof v === 'number' && t(v) } : null
        }
        if (kind === 'select') {
          return want === '(empty)'
            ? (r: T) => { const v = get(r); return v === null || v === undefined || v === '' }
            : (r: T) => String(get(r) ?? '') === want
        }
        const q = want.toLowerCase()
        return (r: T) => String(get(r) ?? '').toLowerCase().includes(q)
      })
      .filter((t): t is (r: T) => boolean => t !== null)
    let out = tests.length ? rows.filter((r) => tests.every((t) => t(r))) : rows
    const key = sort?.replace(/^-/, '')
    const col = columns.find((c) => c.key === key && c.value)
    if (col?.value) {
      const get = col.value
      const dir = sort?.startsWith('-') ? -1 : 1
      out = [...out].sort((a, b) => {
        const x = get(a)
        const y = get(b)
        const xe = x === null || x === undefined || x === ''
        const ye = y === null || y === undefined || y === ''
        if (xe || ye) return xe === ye ? 0 : xe ? 1 : -1        // empty values last, both ways
        if (typeof x === 'number' && typeof y === 'number') return (x - y) * dir
        return String(x).localeCompare(String(y), undefined, { numeric: true }) * dir
      })
    }
    return out
  }, [rows, columns, values, meta, sort])

  const update = (fn: (p: URLSearchParams) => void) =>
    setParams((p) => { const next = new URLSearchParams(p); fn(next); return next }, { replace: true })

  return {
    values,
    sort,
    meta,
    shown,
    active: Object.keys(values).length,
    set: (key: string, value: string) => update((p) => (value ? p.set(prefix + key, value) : p.delete(prefix + key))),
    /** Click on a header: ascending, then descending, then no sort. */
    toggleSort: (key: string) => update((p) => {
      const next = sort === key ? `-${key}` : sort === `-${key}` ? null : key
      if (next) p.set(prefix + 'sort', next)
      else p.delete(prefix + 'sort')
    }),
    clear: () => update((p) => {
      for (const k of Array.from(p.keys())) if (k.startsWith(prefix)) p.delete(k)
    }),
  }
}

export type ColumnFilters<T> = ReturnType<typeof useColumnFilters<T>>
