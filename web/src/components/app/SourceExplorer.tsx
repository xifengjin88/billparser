import { useState } from 'react'

import { useSourceInfo, useSourceLines, useSourceTable } from '@/api/queries'
import { Empty, ErrorBox, Loading, NativeSelect } from '@/components/app/common'
import { Legend, ROLE_CLASS, SourceRows, pageImageUrl } from '@/components/app/source'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'

const PAGE_ROWS = 200

/** The whole original file, colored by what the parser read: PDF pages, table sheets, or text lines. */
export function SourceExplorer({ sha }: { sha: string }) {
  const info = useSourceInfo(sha)
  if (info.isLoading) return <Loading what="Reading the source" />
  if (info.error) return <ErrorBox error={info.error} />
  const s = info.data!
  return (
    <div className="space-y-3">
      <Legend />
      {s.has_pdf ? <PdfPages sha={sha} pages={s.page_count} /> : s.tables.length ? <Tables sha={sha} /> : s.lines ? <Lines sha={sha} /> : <Empty>No stored source text.</Empty>}
    </div>
  )
}

function PdfPages({ sha, pages }: { sha: string; pages: number }) {
  const [page, setPage] = useState(1)
  const [colored, setColored] = useState(true)
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
        <label className="flex items-center gap-1.5 text-sm">
          Page <Input type="number" min={1} max={pages} value={page} onChange={(e) => setPage(Math.min(pages, Math.max(1, Number(e.target.value) || 1)))} className="w-20" /> of {pages}
        </label>
        <Button size="sm" variant="outline" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</Button>
        <label className="ml-2 flex items-center gap-2 text-sm"><Switch checked={colored} onCheckedChange={setColored} /> Color each line</label>
      </div>
      <div className="overflow-auto rounded-lg border bg-white">
        <img key={`${page}-${colored}`} src={pageImageUrl(sha, page, null, colored)} alt={`Page ${page}`} className="mx-auto max-w-full" />
      </div>
    </div>
  )
}

function Tables({ sha }: { sha: string }) {
  const info = useSourceInfo(sha)
  const tables = info.data?.tables ?? []
  const [name, setName] = useState(tables[0]?.name ?? '')
  const [offset, setOffset] = useState(0)
  const q = useSourceTable(sha, name, offset, PAGE_ROWS)
  const total = q.data?.total_rows ?? tables.find((t) => t.name === name)?.rows ?? 0
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {tables.length > 1 && (
          <NativeSelect value={name} onChange={(e) => { setName(e.target.value); setOffset(0) }}>
            {tables.map((t) => <option key={t.name} value={t.name}>{t.name} ({t.rows.toLocaleString()} rows{t.truncated ? '+' : ''})</option>)}
          </NativeSelect>
        )}
        <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_ROWS))}>Previous</Button>
        <span className="text-sm text-muted-foreground tabular-nums">rows {offset + 1}–{Math.min(total, offset + PAGE_ROWS)} of {total.toLocaleString()}</span>
        <Button size="sm" variant="outline" disabled={offset + PAGE_ROWS >= total && !q.data?.truncated} onClick={() => setOffset(offset + PAGE_ROWS)}>Next</Button>
        {q.isFetching && <span className="text-xs text-muted-foreground">loading…</span>}
      </div>
      {q.error ? <ErrorBox error={q.error} /> : q.data ? <SourceRows table={q.data} /> : <Loading />}
      {q.data?.truncated && !q.data.from_file && (
        <p className="text-xs text-muted-foreground">The stored copy keeps the first 5,000 rows; later rows are read from the uploaded file.</p>
      )}
    </div>
  )
}

function Lines({ sha }: { sha: string }) {
  const q = useSourceLines(sha, null)
  if (q.isLoading) return <Loading />
  if (q.error) return <ErrorBox error={q.error} />
  return (
    <div className="max-h-[640px] overflow-auto rounded-lg border font-mono text-xs">
      {(q.data ?? []).slice(0, 5000).map((l) => (
        <div key={`${l.page}-${l.line}`} className={cn('flex gap-3 px-2 py-0.5 whitespace-pre', ROLE_CLASS[l.role])}>
          <span className="w-16 shrink-0 text-right text-muted-foreground">{l.page}:{l.line}</span>
          <span>{l.text}</span>
        </div>
      ))}
    </div>
  )
}
