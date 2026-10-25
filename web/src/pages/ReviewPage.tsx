import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { Loader2Icon, PlusIcon } from 'lucide-react'

import type { ChargeRow, CiteOut, DocumentView, GapRow, HeaderEdit, ReviewEdits } from '@/api/client'
import { useApprove, useDocumentView, usePreview, useSettings } from '@/api/queries'
import { CompletenessPill, ConfidencePill, Pill, StatusPill } from '@/components/app/badges'
import { ChargeFilters, useChargeFilter } from '@/components/app/charges'
import { DataTable, type Column } from '@/components/app/DataTable'
import { ErrorBox, Loading, NativeSelect, PageHeader, Section } from '@/components/app/common'
import { SourceView } from '@/components/app/source'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { label, money } from '@/lib/format'
import { cn } from '@/lib/utils'

type RowEdit = Partial<{ description: string; cost: number | null; kind: string; counted: boolean; service: string; service_code: string; delete: boolean }>
type Added = { key: string; description: string; cost: number | null; kind: string; service: string; service_code: string; counted: boolean }

export function ReviewPage() {
  const { sha = '' } = useParams()
  const view = useDocumentView(sha)
  if (view.isLoading) return <Loading what="Loading the document" />
  if (view.error) return <ErrorBox error={view.error} />
  return <ReviewBody key={sha} stored={view.data!} />
}

function ReviewBody({ stored }: { stored: DocumentView }) {
  const sha = stored.sha256
  const settings = useSettings()
  const navigate = useNavigate()
  const preview = usePreview(sha)
  const approve = useApprove(sha)
  const [rows, setRows] = useState<Record<string, RowEdit>>({})
  const [added, setAdded] = useState<Added[]>([])
  const [dropped, setDropped] = useState<number[]>([])
  const [header, setHeader] = useState<HeaderEdit>({})
  const [dirty, setDirty] = useState(false)
  const [version, setVersion] = useState(0)            // remounts the inputs after a discard
  const [ticks, setTicks] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [cite, setCite] = useState<CiteOut | null>(stored.gaps[0]?.cite ?? stored.charges[0]?.cite ?? null)
  const current = preview.data?.view ?? stored
  const filter = useChargeFilter(stored.charges)
  const kinds = settings.data?.kinds ?? []

  const edit = (id: string, patch: RowEdit) => {
    setRows((r) => ({ ...r, [id]: { ...r[id], ...patch } }))
    setDirty(true)
  }
  const edits = (): ReviewEdits => ({
    rows,
    added: added.filter((a) => a.description || a.cost !== null || a.service_code)
      .map(({ key, ...a }) => ({ ...a, id: key, delete: false })),
    dropped_gaps: dropped,
    header,
  })
  const recheck = () =>
    preview.mutate(edits(), { onSuccess: () => setDirty(false), onError: (e) => toast.error(e.message) })
  const discard = () => {
    setRows({}); setAdded([]); setDropped([]); setHeader({}); setDirty(false); preview.reset(); setVersion((v) => v + 1)
  }
  const waiting = Object.keys(rows).length + added.length + dropped.length + Object.keys(header).length
  const checklist = settings.data?.checklist ?? []
  const allTicked = checklist.length > 0 && checklist.every((c) => ticks.includes(c.key))

  /** A gap's code applies to every charge with the same printed service that has no code yet. */
  const applyCode = (g: GapRow, choice: string) => {
    const code = choice.split(' — ')[0].trim()
    const ref = stored.charges.find((c) => c.id === g.ref)
    const labelOf = (c: ChargeRow) => c.service ?? c.description
    const target = ref ? labelOf(ref) : null
    const next = { ...rows }
    for (const c of stored.charges) {
      if ((c.id === g.ref || (target && labelOf(c) === target)) && !c.service_code) next[c.id] = { ...next[c.id], service_code: code }
    }
    setRows(next)
    setDirty(true)
    setVersion((v) => v + 1)
  }

  const columns: Column<ChargeRow>[] = useMemo(() => {
    const val = <K extends keyof RowEdit>(c: ChargeRow, k: K, fallback: RowEdit[K]) => (rows[c.id]?.[k] ?? fallback) as RowEdit[K]
    const text = (k: 'description' | 'service' | 'service_code', mono = false): Column<ChargeRow> => ({
      key: k, header: label(k), width: k === 'description' ? 'minmax(240px, 3fr)' : k === 'service' ? 'minmax(150px, 1.5fr)' : '150px',
      render: (c) => (
        <Input key={`${c.id}-${version}`} defaultValue={String(val(c, k, c[k] ?? '') ?? '')} className={cn('h-7 px-1.5', mono && 'font-mono text-xs')}
          onBlur={(e) => { if (e.target.value !== (c[k] ?? '')) edit(c.id, { [k]: e.target.value }) }}
          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }} />
      ),
    })
    return [
      { key: 'delete', header: 'Del', width: '44px', align: 'center', render: (c) => <Checkbox checked={!!val(c, 'delete', false)} onCheckedChange={(v) => edit(c.id, { delete: !!v })} aria-label="Delete this row" /> },
      text('description'),
      {
        key: 'cost', header: 'Cost', width: '120px', align: 'right',
        render: (c) => (
          <Input key={`${c.id}-${version}`} defaultValue={val(c, 'cost', c.cost) ?? ''} inputMode="decimal" className="h-7 px-1.5 text-right tabular-nums"
            onBlur={(e) => { const n = e.target.value === '' ? null : Number(e.target.value); if (n !== c.cost && !Number.isNaN(n)) edit(c.id, { cost: n }) }}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }} />
        ),
      },
      {
        key: 'kind', header: 'Kind', width: '160px',
        render: (c) => (
          <NativeSelect value={val(c, 'kind', c.kind)} onChange={(e) => edit(c.id, { kind: e.target.value })} className="h-7 w-full">
            {kinds.map((k) => <option key={k} value={k}>{label(k)}</option>)}
          </NativeSelect>
        ),
      },
      { key: 'counted', header: 'Counted', width: '72px', align: 'center', render: (c) => <Checkbox checked={!!val(c, 'counted', c.counted)} onCheckedChange={(v) => edit(c.id, { counted: !!v })} aria-label="Counted" /> },
      text('service'),
      text('service_code', true),
      { key: 'src', header: 'Source', width: '170px', render: (c) => <button className="truncate text-xs text-muted-foreground hover:underline" onClick={() => setCite(c.cite)}>{c.source}</button> },
    ]
  }, [rows, version, kinds]) // eslint-disable-line react-hooks/exhaustive-deps

  const h = { ...header }
  return (
    <>
      <PageHeader
        title={<>Review · {stored.name}</>}
        description={
          <span className="flex flex-wrap items-center gap-1.5">
            <StatusPill status={stored.status} /> <CompletenessPill value={current.completeness} /> <ConfidencePill level={current.confidence?.level} />
            <Pill>counted total {money(current.counted_total)}</Pill>
            {preview.data && <Pill t="blue">after your edits (not saved)</Pill>}
          </span>
        }
        actions={<Link to={`/documents/${sha}`} className="text-sm text-muted-foreground hover:underline">Open the result</Link>}
      />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(360px,2fr)]">
        <div className="min-w-0 space-y-6">
          <Section title="Gaps">
            <Gaps stored={stored} current={current} dropped={dropped}
              onDrop={(i) => { setDropped((d) => d.includes(i) ? d.filter((x) => x !== i) : [...d, i]); setDirty(true) }}
              onApply={applyCode} onShow={setCite} />
          </Section>

          <Section title="Document">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <Field label="Type">
                <NativeSelect className="w-full" value={h.doc_type ?? stored.doc_type ?? 'other'} onChange={(e) => { setHeader({ ...header, doc_type: e.target.value }); setDirty(true) }}>
                  {(settings.data?.doc_types ?? []).map((t) => <option key={t} value={t}>{label(t)}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Provider">
                <NativeSelect className="w-full" value={h.provider ?? stored.provider ?? 'other'} onChange={(e) => { setHeader({ ...header, provider: e.target.value }); setDirty(true) }}>
                  {['aws', 'azure', 'gcp', 'other'].map((p) => <option key={p} value={p}>{p}</option>)}
                </NativeSelect>
              </Field>
              {(['currency', 'period_start', 'period_end'] as const).map((k) => (
                <Field key={k} label={label(k)}>
                  <Input key={`${k}-${version}`} defaultValue={h[k] ?? stored[k] ?? ''} className="h-8"
                    onBlur={(e) => { if (e.target.value !== (stored[k] ?? '')) { setHeader({ ...header, [k]: e.target.value }); setDirty(true) } }} />
                </Field>
              ))}
              <Field label="Basis">
                <NativeSelect className="w-full" value={h.basis ?? ''} onChange={(e) => { setHeader({ ...header, basis: (e.target.value || null) as HeaderEdit['basis'] }); setDirty(true) }}>
                  <option value="">as parsed</option>
                  {['billed', 'estimated', 'priced'].map((b) => <option key={b} value={b}>{b}</option>)}
                </NativeSelect>
              </Field>
            </div>
          </Section>

          <Section title="Charges" actions={
            <Button size="sm" variant="outline" onClick={() => setAdded([...added, { key: `new-${Date.now()}`, description: '', cost: null, kind: 'usage', service: '', service_code: '', counted: true }])}>
              <PlusIcon /> Add a charge
            </Button>
          }>
            <ChargeFilters {...filter} total={stored.charges.length} />
            {added.length > 0 && (
              <div className="space-y-1 rounded-lg border border-dashed p-2">
                {added.map((a, i) => (
                  <div key={a.key} className="flex flex-wrap items-center gap-2">
                    <Input placeholder="Description" defaultValue={a.description} className="h-7 w-64" onBlur={(e) => { const n = [...added]; n[i] = { ...a, description: e.target.value }; setAdded(n); setDirty(true) }} />
                    <Input placeholder="Cost" defaultValue={a.cost ?? ''} inputMode="decimal" className="h-7 w-28" onBlur={(e) => { const n = [...added]; n[i] = { ...a, cost: e.target.value === '' ? null : Number(e.target.value) }; setAdded(n); setDirty(true) }} />
                    <Input placeholder="Service code" defaultValue={a.service_code} className="h-7 w-40 font-mono text-xs" onBlur={(e) => { const n = [...added]; n[i] = { ...a, service_code: e.target.value }; setAdded(n); setDirty(true) }} />
                    <Button size="sm" variant="ghost" onClick={() => setAdded(added.filter((x) => x !== a))}>Remove</Button>
                  </div>
                ))}
              </div>
            )}
            <DataTable rows={filter.shown} rowKey={(c) => c.id} columns={columns} height={460} rowHeight={38}
              rowClassName={(c) => cn(rows[c.id]?.delete && 'opacity-50 line-through', rows[c.id] && !rows[c.id]?.delete && 'bg-sky-50/70 dark:bg-sky-950/40')} />
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={recheck} disabled={preview.isPending}>{preview.isPending && <Loader2Icon className="animate-spin" />} Check the totals again</Button>
              <Button variant="outline" onClick={discard} disabled={!waiting && !preview.data}>Discard my edits</Button>
              {waiting > 0 && <span className="text-sm text-muted-foreground">{waiting} change(s){dirty ? ' — not checked yet' : ''}</span>}
            </div>
          </Section>

          {current.totals.length > 0 && (
            <Section title="Totals">
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full text-sm">
                  <tbody>
                    {current.totals.map((t) => (
                      <tr key={t.id} className="border-b last:border-0">
                        <td className="px-3 py-1.5">{t.label}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{money(t.printed)}</td>
                        <td className="px-3 py-1.5 text-right text-muted-foreground tabular-nums">{money(t.computed)}</td>
                        <td className="px-3 py-1.5">{t.status === 'match' ? '✓' : t.status === 'mismatch' ? <span className="text-red-600">✗</span> : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          )}

          <Section title="Checklist">
            <div className="space-y-2 rounded-lg border p-3">
              {checklist.map((c) => (
                <label key={c.key} className="flex items-start gap-2 text-sm">
                  <Checkbox className="mt-0.5" checked={ticks.includes(c.key)} onCheckedChange={(v) => setTicks(v ? [...ticks, c.key] : ticks.filter((x) => x !== c.key))} />
                  {c.text}
                </label>
              ))}
              <Input placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
              {current.completeness === 'contradicted' && (
                <p className="text-sm text-amber-800 dark:text-amber-300">The totals still do not tie. Fix the lines, or approve anyway if the printed total itself is wrong (say so in the note).</p>
              )}
              {dirty && <p className="text-sm text-amber-800 dark:text-amber-300">Check the totals again first: your edits are applied only then.</p>}
              <Button disabled={!allTicked || dirty || approve.isPending}
                onClick={() => approve.mutate({ ...edits(), note, checklist: ticks }, {
                  onSuccess: () => { toast.success(`Approved ${stored.name}`); navigate(`/documents/${sha}`) },
                  onError: (e) => toast.error(e.message),
                })}>
                {approve.isPending && <Loader2Icon className="animate-spin" />} Approve
              </Button>
            </div>
          </Section>
        </div>
        <div className="min-w-0">
          <div className="sticky top-4 space-y-2">
            <h2 className="text-sm font-semibold">Source</h2>
            <SourceView sha={sha} cite={cite} />
          </div>
        </div>
      </div>
    </>
  )
}

function Field({ label: l, children }: { label: string; children: React.ReactNode }) {
  return <label className="space-y-1 text-xs text-muted-foreground"><span>{l}</span>{children}</label>
}

function Gaps({ stored, current, dropped, onDrop, onApply, onShow }: {
  stored: DocumentView; current: DocumentView; dropped: number[]
  onDrop: (i: number) => void; onApply: (g: GapRow, choice: string) => void; onShow: (c: CiteOut) => void
}) {
  const [choice, setChoice] = useState<Record<number, string>>({})
  // gaps a person can mark resolved are those of the stored document (their index is sent); service-code gaps
  // follow the edits, so they come from the checked version
  const droppable = stored.gaps.filter((g) => ['unclaimed_amount', 'failed_window', 'no_bill_table'].includes(g.kind))
  const codes = current.gaps.filter((g) => g.kind === 'missing_match_field')
  const other = current.gaps.filter((g) => !['unclaimed_amount', 'failed_window', 'no_bill_table', 'missing_match_field'].includes(g.kind))
  if (!droppable.length && !codes.length && !other.length && !current.flags.length) return <div className="text-sm text-emerald-700 dark:text-emerald-400">No gaps.</div>
  return (
    <div className="space-y-2">
      {droppable.map((g) => (
        <div key={`d${g.index}`} className={cn('flex flex-wrap items-start justify-between gap-2 rounded-lg border p-3 text-sm', dropped.includes(g.index) && 'opacity-60')}>
          <div className="min-w-0 flex-1"><span className="font-medium">{label(g.kind)}</span> — {g.message}</div>
          <div className="flex gap-1.5">
            {g.cite && <Button size="sm" variant="ghost" onClick={() => onShow(g.cite!)}>Show</Button>}
            <Button size="sm" variant={dropped.includes(g.index) ? 'secondary' : 'outline'} onClick={() => onDrop(g.index)}>
              {dropped.includes(g.index) ? 'Undo' : g.kind === 'unclaimed_amount' ? 'Not money' : 'Accept'}
            </Button>
          </div>
        </div>
      ))}
      {codes.map((g) => (
        <div key={`c${g.index}`} className="flex flex-wrap items-start justify-between gap-2 rounded-lg border p-3 text-sm">
          <div className="min-w-0 flex-1"><span className="font-medium">{label(g.kind)}</span> — {g.message}</div>
          {g.field === 'service_code' && g.ref && (
            <div className="flex gap-1.5">
              <Input list={`cand-${g.index}`} placeholder="Service code" className="h-8 w-48 font-mono text-xs" value={choice[g.index] ?? ''}
                onChange={(e) => setChoice({ ...choice, [g.index]: e.target.value })} />
              <datalist id={`cand-${g.index}`}>{g.candidates.map((c) => <option key={c} value={c} />)}</datalist>
              <Button size="sm" variant="outline" disabled={!choice[g.index]} onClick={() => onApply(g, choice[g.index])}>Apply</Button>
            </div>
          )}
        </div>
      ))}
      {other.map((g) => <div key={`o${g.index}`} className="rounded-lg border p-3 text-sm"><span className="font-medium">{label(g.kind)}</span> — {g.message}</div>)}
      {current.flags.map((f, i) => <div key={`f${i}`} className="text-xs text-amber-800 dark:text-amber-300">⚠ {f.message}{f.where ? ` — ${f.where}` : ''}</div>)}
    </div>
  )
}
