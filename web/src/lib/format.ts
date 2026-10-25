const money2 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const usd4 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 4, maximumFractionDigits: 4 })

/** An amount with two decimals; the API sends exact amounts as strings. */
export function money(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? money2.format(n) : String(v)
}

export function usd(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : usd4.format(v)
}

export function when(ts: number | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(1)} s`
}

/** A duration in seconds: 44.2 s, 2 min 48 s. */
export function took(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`
}

/** The time of an upload: the whole of it (a learning job included) when known, else its parse. */
export function uploadTime(total: number | null | undefined, parse: number | null | undefined): string {
  return ms(total ?? parse)
}

export const label = (s: string | null | undefined) => (s ? s.replaceAll('_', ' ') : '—')

/** A file size as people read it: 820 B, 42 KB, 4.2 MB. */
export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
