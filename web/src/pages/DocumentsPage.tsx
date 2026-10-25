import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { toast } from 'sonner'
import { Trash2Icon } from 'lucide-react'

import type { DocumentItem } from '@/api/client'
import { useDeleteDocument, useDocuments } from '@/api/queries'
import { CompletenessPill, StatusPill } from '@/components/app/badges'
import { DataTable } from '@/components/app/DataTable'
import { ErrorBox, Loading, NativeSelect, PageHeader } from '@/components/app/common'
import { ConfirmDelete } from '@/components/app/ConfirmDelete'
import { Input } from '@/components/ui/input'
import { money, ms, uploadTime, when } from '@/lib/format'

export function DocumentsPage() {
  const docs = useDocuments()
  const del = useDeleteDocument()
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const [who, setWho] = useState('')
  const [toDelete, setToDelete] = useState<DocumentItem | null>(null)
  const rows = useMemo(
    () => (docs.data ?? []).filter((d) => (!status || d.status === status) && (!who || (d.uploaded_by ?? '') === who)
      && (!q || d.name.toLowerCase().includes(q.toLowerCase()))),
    [docs.data, q, status, who],
  )
  return (
    <>
      <PageHeader title="Documents" description="Every parsed file, newest first. Open one to see its charges, totals, gaps and source." />
      <Uploaders docs={docs.data ?? []} who={who} onPick={setWho} />
      <div className="mb-3 flex flex-wrap gap-2">
        <Input placeholder="Search file name" value={q} onChange={(e) => setQ(e.target.value)} className="w-64" />
        <NativeSelect value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {['auto_accepted', 'needs_review', 'approved', 'processing', 'interrupted'].map((s) => <option key={s} value={s}>{s.replaceAll('_', ' ')}</option>)}
        </NativeSelect>
        <span className="self-center text-sm text-muted-foreground">{rows.length} of {docs.data?.length ?? 0}</span>
      </div>
      {docs.isLoading ? <Loading /> : docs.error ? <ErrorBox error={docs.error} /> : (
        <DataTable
          rows={rows}
          rowKey={(d) => d.sha256}
          height={620}
          empty="No document matches."
          columns={[
            { key: 'name', header: 'File', width: 'minmax(260px, 3fr)', render: (d) => <Link className="hover:underline" to={`/documents/${d.sha256}`}>{d.name}</Link> },
            { key: 'status', header: 'Status', width: '130px', render: (d) => <StatusPill status={d.status} /> },
            { key: 'completeness', header: 'Totals', width: '120px', render: (d) => <CompletenessPill value={d.completeness} /> },
            { key: 'lane', header: 'Read by', width: 'minmax(180px, 2fr)', render: (d) => <span className="text-muted-foreground">{d.lane}</span> },
            { key: 'risk', header: 'Money at risk', width: '120px', align: 'right', render: (d) => money(d.money_at_risk) },
            { key: 'by', header: 'Uploaded by', width: 'minmax(160px, 1.5fr)', render: (d) => <span className="text-muted-foreground">{d.uploaded_by ?? '—'}</span> },
            {
              key: 'ms', header: 'Time', width: '90px', align: 'right',
              render: (d) => <span title={`Whole upload ${ms(d.total_ms)} · last parse ${ms(d.elapsed_ms)}`}>{uploadTime(d.total_ms, d.elapsed_ms)}</span>,
            },
            { key: 'calls', header: 'Model calls', width: '100px', align: 'right', render: (d) => d.model_calls ?? '—' },
            { key: 'when', header: 'Parsed', width: '150px', render: (d) => when(d.uploaded_at) },
            {
              key: 'del', header: '', width: '44px', align: 'center',
              render: (d) => (
                <button aria-label={`Delete ${d.name}`} className="text-muted-foreground hover:text-red-600" onClick={() => setToDelete(d)}>
                  <Trash2Icon className="size-4" />
                </button>
              ),
            },
          ]}
        />
      )}
      <ConfirmDelete
        open={!!toDelete}
        name={toDelete?.name ?? ''}
        pending={del.isPending}
        onCancel={() => setToDelete(null)}
        onConfirm={() => toDelete && del.mutate(toDelete.sha256, {
          onSuccess: () => { toast.success(`Deleted ${toDelete.name}`); setToDelete(null) },
          onError: (e) => toast.error(e.message),
        })}
      />
    </>
  )
}

/** Who uploaded what: one row per person (a file's first uploader); a click filters the list to them. */
function Uploaders({ docs, who, onPick }: { docs: DocumentItem[]; who: string; onPick: (who: string) => void }) {
  const people = new Map<string, { count: number; last: number }>()
  for (const d of docs) {
    const key = d.uploaded_by ?? ''
    const p = people.get(key) ?? { count: 0, last: 0 }
    people.set(key, { count: p.count + 1, last: Math.max(p.last, d.uploaded_at ?? 0) })
  }
  if (!docs.length) return null
  const rows = [...people.entries()].sort((a, b) => b[1].count - a[1].count)
  return (
    <section className="mb-5 space-y-2">
      <h2 className="text-sm font-semibold">Uploads by person</h2>
      <div className="flex flex-wrap gap-2">
        {rows.map(([email, p]) => (
          <button
            key={email || 'unknown'}
            onClick={() => onPick(who === email ? '' : email)}
            className={`rounded-lg border px-3 py-2 text-left text-sm hover:bg-muted/60 ${who === email ? 'border-sky-500 bg-sky-50 dark:bg-sky-950/50' : 'bg-card'}`}
          >
            <div className="font-medium">{email || 'Not recorded'}</div>
            <div className="text-xs text-muted-foreground">{p.count} file{p.count === 1 ? '' : 's'} · last {when(p.last)}</div>
          </button>
        ))}
        {who !== '' && <button className="self-center text-sm text-muted-foreground hover:underline" onClick={() => onPick('')}>Show everyone</button>}
      </div>
      {people.has('') && <p className="text-xs text-muted-foreground">“Not recorded”: uploaded with sign-in off, or before uploads were recorded.</p>}
    </section>
  )
}
