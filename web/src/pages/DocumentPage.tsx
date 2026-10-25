import { useEffect, useRef } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { DownloadIcon, FileCheckIcon, Loader2Icon, RotateCwIcon, Trash2Icon } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError, downloadJson, type DocumentItem, type DocumentView } from '@/api/client'
import { keys, useDeleteDocument, useDocumentView, useParseCurrent, useReparse, useStopDocument, useTask } from '@/api/queries'
import { CompletenessPill, ConfidencePill, Pill, StatusPill } from '@/components/app/badges'
import { ChargesTab } from '@/components/app/charges'
import { ConfirmDelete } from '@/components/app/ConfirmDelete'
import { DataTable } from '@/components/app/DataTable'
import { Empty, ErrorBox, JsonBlock, Loading, PageHeader, Section, Stat } from '@/components/app/common'
import { SourceExplorer } from '@/components/app/SourceExplorer'
import { Button, buttonVariants } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { label, money, ms, uploadTime, usd, when } from '@/lib/format'

export function DocumentPage() {
  const { sha = '' } = useParams()
  const view = useDocumentView(sha)
  const busy = view.error instanceof ApiError && view.error.status === 409
  if (view.isLoading) return <Loading what="Loading the document" />
  if (busy) return <Processing sha={sha} />
  if (view.error) return <ErrorBox error={view.error} onRetry={() => view.refetch()} />
  return <DocumentBody view={view.data!} />
}

/** A document with no result: being parsed now (show what the job does, then the result), or a parse that stopped
before it saved one (interrupted). The stored status decides, not the missing result alone. */
function Processing({ sha }: { sha: string }) {
  const qc = useQueryClient()
  const task = useTask(sha)
  const docs = useQuery({
    queryKey: keys.documents,
    queryFn: () => api.get<DocumentItem[]>('/documents'),
    refetchInterval: (q) => (q.state.data?.find((d) => d.sha256 === sha)?.status === 'processing' ? 3000 : false),
  })
  const reparse = useReparse()
  const stop = useStopDocument()
  const row = docs.data?.find((d) => d.sha256 === sha)
  const running = task.data?.state === 'running' || row?.status === 'processing'
  // ask for the result again once, when a parse that was running ends (never in a loop: a file that has no
  // result and no parse running stays on this page until a person parses it again)
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !running) qc.invalidateQueries({ queryKey: keys.view(sha) })
    wasRunning.current = running
  }, [running, qc, sha])
  if (docs.isLoading) return <Loading what="Loading the document" />
  if (running) {
    return (
      <div className="space-y-3">
        <PageHeader
          title={task.data?.name ?? row?.name ?? 'Parsing'}
          actions={
            <Button variant="destructive" size="sm" disabled={stop.isPending}
              title="Stops the learning or improving run of this file at its next step. The file keeps the result it had."
              onClick={() => stop.mutate(sha, {
                onSuccess: (r) => toast.success(r.stopped.length ? 'Stopping: the run ends at its next step.' : 'No learning run is running for this file.'),
                onError: (e) => toast.error(e.message),
              })}>
              Stop
            </Button>
          }
        />
        <div className="flex items-center gap-2 rounded-lg border p-4 text-sm text-violet-800 dark:text-violet-300">
          <Loader2Icon className="size-4 animate-spin" /> {task.data?.progress ?? 'This file is being parsed. Its result appears here when the parse ends.'}
        </div>
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <PageHeader title={row?.name ?? 'Document'} description={row && <StatusPill status={row.status} />} />
      <div className="space-y-3 rounded-lg border p-4 text-sm">
        <p>
          {row?.status === 'interrupted'
            ? 'This file has no result: its last parse stopped before it saved one.'
            : 'This file has no result yet.'}
        </p>
        <Button variant="outline" size="sm" disabled={reparse.isPending}
          title="Reads the file again and runs the agent for it again (paid when learning is on)"
          onClick={() => reparse.mutate(sha, { onError: (e) => toast.error(e.message) })}>
          {reparse.isPending ? <Loader2Icon className="animate-spin" /> : <RotateCwIcon />} Parse again
        </Button>
      </div>
    </div>
  )
}

function DocumentBody({ view }: { view: DocumentView }) {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') ?? 'charges'
  const navigate = useNavigate()
  const qc = useQueryClient()
  const reparse = useReparse()
  const parseCurrent = useParseCurrent()
  const stop = useStopDocument()
  const del = useDeleteDocument()
  const [confirm, setConfirm] = useState(false)
  const [waiting, setWaiting] = useState<'agent' | 'spec' | null>(null)
  const task = useTask(view.sha256, waiting !== null)

  useEffect(() => {
    if (waiting && task.data && task.data.state !== 'running') {
      setWaiting(null)
      if (task.data.state === 'error') toast.error(task.data.error ?? 'The parse failed.')
      else toast.success(waiting === 'agent' ? 'Parsed again; the agent ran for this file.' : 'Parsed again with the current specs.')
      qc.invalidateQueries({ queryKey: keys.view(view.sha256) })
      qc.invalidateQueries({ queryKey: keys.documents })
    }
  }, [waiting, task.data, qc, view.sha256])

  const v = view
  return (
    <>
      <PageHeader
        title={v.name}
        description={
          <span className="flex flex-wrap items-center gap-1.5">
            <StatusPill status={v.status} /> <CompletenessPill value={v.completeness} /> <ConfidencePill level={v.confidence?.level} />
            <Pill>{v.lane}</Pill>
            {v.specs_used.map((sv) => <Pill key={sv}><span title="The spec version that read this file">{sv}</span></Pill>)}
            <Pill><span title={`Last parse ${ms(v.elapsed_ms)}`}>took {uploadTime(v.total_ms, v.elapsed_ms)}</span></Pill>
            <Pill>{v.model_calls ?? 0} model calls</Pill>
            {v.uploaded_by && <Pill>uploaded by {v.uploaded_by}</Pill>}
            {v.showing_reviewed && <Pill t="blue">showing the reviewed version</Pill>}
          </span>
        }
        actions={
          <>
            <Link to={`/review/${v.sha256}`} className={buttonVariants({ variant: 'outline', size: 'sm' })}>Review</Link>
            <Button variant="outline" size="sm" disabled={waiting !== null || parseCurrent.isPending}
              title="Reads the file again with the specs and code of today: no agent and no model request (free)"
              onClick={() => parseCurrent.mutate(v.sha256, { onSuccess: () => setWaiting('spec'), onError: (e) => toast.error(e.message) })}>
              {waiting === 'spec' ? <Loader2Icon className="animate-spin" /> : <FileCheckIcon />} Parse with current spec
            </Button>
            <Button variant="outline" size="sm" disabled={waiting !== null || reparse.isPending}
              title="Reads the file again and runs the agent for it again: a new layout spec, or a fresh look at the one that reads it (paid when learning is on)"
              onClick={() => reparse.mutate(v.sha256, { onSuccess: () => setWaiting('agent'), onError: (e) => toast.error(e.message) })}>
              {waiting === 'agent' ? <Loader2Icon className="animate-spin" /> : <RotateCwIcon />} Parse again (agent)
            </Button>
            <Button variant="outline" size="sm" onClick={() => downloadJson(`/documents/${v.sha256}`, `${v.name}.json`).catch((e) => toast.error(e.message))}>
              <DownloadIcon /> JSON
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirm(true)}><Trash2Icon /> Delete</Button>
          </>
        }
      />
      {waiting && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border p-3 text-sm text-violet-800 dark:text-violet-300">
          <Loader2Icon className="size-4 animate-spin" /> <span className="flex-1">{task.data?.progress ?? 'Parsing again…'}</span>
          {waiting === 'agent' && (
            <Button variant="destructive" size="sm" disabled={stop.isPending}
              title="Stops the agent's run at its next step. The file keeps the result it had."
              onClick={() => stop.mutate(v.sha256, {
                onSuccess: (r) => toast.success(r.stopped.length ? 'Stopping: the run ends at its next step.' : 'No agent run is running for this file.'),
                onError: (e) => toast.error(e.message),
              })}>
              Stop
            </Button>
          )}
        </div>
      )}

      {v.confidence && (
        <div className={`mb-4 rounded-lg border px-3 py-2 text-sm ${v.confidence.level === 'low' ? 'border-red-200 bg-red-50/60 dark:border-red-900 dark:bg-red-950/40' : v.confidence.level === 'medium' ? 'border-amber-200 bg-amber-50/60 dark:border-amber-900 dark:bg-amber-950/40' : 'bg-emerald-50/50 dark:bg-emerald-950/30'}`}>
          <span className="font-medium">Confidence {v.confidence.level}.</span>{' '}
          {v.confidence.reasons.join(' ')}
        </div>
      )}

      <div className="mb-5 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Type" value={label(v.doc_type)} />
        <Stat label="Provider" value={v.provider ?? '—'} />
        <Stat label="Currency" value={v.currency ?? '—'} />
        <Stat label="Period" value={v.period_start && v.period_end ? `${v.period_start} → ${v.period_end}` : '—'} />
        <Stat label="Counted total" value={v.charges.length ? money(v.counted_total) : '—'} />
        <Stat label="Money at risk" value={money(v.money_at_risk)} hint="Amounts of open gaps plus the charges they point at" />
      </div>

      <Tabs value={tab} onValueChange={(t) => setParams({ tab: String(t) }, { replace: true })}>
        <TabsList className="flex-wrap">
          <TabsTrigger value="charges">Charges ({v.charges.length.toLocaleString()})</TabsTrigger>
          <TabsTrigger value="totals">Totals ({v.totals.length})</TabsTrigger>
          <TabsTrigger value="gaps">Gaps ({v.gaps.length + v.flags.length})</TabsTrigger>
          <TabsTrigger value="facts">Facts ({v.facts.length})</TabsTrigger>
          <TabsTrigger value="commitments">Commitments ({v.commitments.length})</TabsTrigger>
          <TabsTrigger value="source">Source</TabsTrigger>
          <TabsTrigger value="runs">Runs ({v.runs.length})</TabsTrigger>
          <TabsTrigger value="how">How it was read</TabsTrigger>
        </TabsList>
        <TabsContent value="charges" className="pt-3"><ChargesTab view={v} /></TabsContent>
        <TabsContent value="totals" className="pt-3"><TotalsTab view={v} /></TabsContent>
        <TabsContent value="gaps" className="pt-3"><GapsTab view={v} /></TabsContent>
        <TabsContent value="facts" className="pt-3"><FactsTab view={v} /></TabsContent>
        <TabsContent value="commitments" className="pt-3"><CommitmentsTab view={v} /></TabsContent>
        <TabsContent value="source" className="pt-3"><SourceExplorer sha={v.sha256} /></TabsContent>
        <TabsContent value="runs" className="pt-3"><RunsTab view={v} /></TabsContent>
        <TabsContent value="how" className="pt-3"><HowTab view={v} /></TabsContent>
      </Tabs>

      <ConfirmDelete open={confirm} name={v.name} pending={del.isPending} onCancel={() => setConfirm(false)}
        onConfirm={() => del.mutate(v.sha256, {
          onSuccess: () => { toast.success(`Deleted ${v.name}`); navigate('/documents') },
          onError: (e) => toast.error(e.message),
        })} />
    </>
  )
}

const CHECK: Record<string, string> = { match: '✓ ties', mismatch: '✗ off', untested: '—' }

export function TotalsTab({ view }: { view: DocumentView }) {
  return (
    <div className="space-y-5">
      {view.totals.length ? (
        <DataTable rows={view.totals} rowKey={(t) => t.id} height="auto" columns={[
          { key: 'label', header: 'Total', width: 'minmax(220px, 3fr)', render: (t) => t.label },
          { key: 'printed', header: 'Printed', width: '130px', align: 'right', render: (t) => money(t.printed) },
          { key: 'computed', header: 'Lines add to', width: '130px', align: 'right', render: (t) => money(t.computed) },
          { key: 'check', header: 'Check', width: '90px', render: (t) => <span className={t.status === 'mismatch' ? 'text-red-700 dark:text-red-400' : t.status === 'match' ? 'text-emerald-700 dark:text-emerald-400' : ''}>{CHECK[t.status] ?? t.status}</span> },
          { key: 'level', header: 'Level', width: '100px', render: (t) => t.level },
          { key: 'lines', header: 'Lines', width: '70px', align: 'right', render: (t) => t.lines },
          { key: 'where', header: 'Where', width: '200px', render: (t) => <span className="text-xs text-muted-foreground">{t.cite.label}</span> },
        ]} />
      ) : <Empty>The file prints no totals, so nothing could be reconciled.</Empty>}
      {view.notes.length > 0 && (
        <Section title="Reconciliation notes">
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {view.notes.map((n, i) => <li key={i}>{n.message}{n.where && <span className="text-muted-foreground"> ({n.where})</span>}</li>)}
          </ul>
        </Section>
      )}
    </div>
  )
}

export function GapsTab({ view }: { view: DocumentView }) {
  if (!view.gaps.length && !view.flags.length) return <Empty>No gaps.</Empty>
  return (
    <div className="space-y-2">
      {view.gaps.map((g) => (
        <div key={g.index} className="rounded-lg border p-3 text-sm">
          <div><span className="font-medium">{label(g.kind)}</span> — {g.message}</div>
          {g.candidates.length > 0 && <div className="mt-1 text-xs text-muted-foreground">Candidates: {g.candidates.join('; ')}</div>}
          {g.cite && <div className="mt-1 text-xs text-muted-foreground">Where: {g.cite.label}</div>}
        </div>
      ))}
      {view.flags.length > 0 && (
        <Section title="Uncertain decisions">
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {view.flags.map((f, i) => <li key={i}>{f.message}{f.where && <span className="text-muted-foreground"> ({f.where})</span>}</li>)}
          </ul>
        </Section>
      )}
    </div>
  )
}

function FactsTab({ view }: { view: DocumentView }) {
  if (!view.facts.length) return <Empty>No figures stated in prose.</Empty>
  return (
    <DataTable rows={view.facts} rowKey={(_, i) => String(i)} height="auto" columns={[
      { key: 'label', header: 'Label', width: 'minmax(200px, 2fr)', render: (f) => f.label },
      { key: 'amount', header: 'Amount', width: '130px', align: 'right', render: (f) => money(f.amount) },
      { key: 'cur', header: 'Currency', width: '90px', render: (f) => f.currency ?? '—' },
      { key: 'period', header: 'Period', width: '140px', render: (f) => f.period ?? '—' },
      { key: 'sentence', header: 'Sentence', width: 'minmax(260px, 3fr)', render: (f) => <span className="text-muted-foreground" title={f.cite.text ?? ''}>{f.cite.text}</span> },
    ]} />
  )
}

function CommitmentsTab({ view }: { view: DocumentView }) {
  if (!view.commitments.length) return <Empty>No Reserved Instances or Savings Plans in this file.</Empty>
  return (
    <DataTable rows={view.commitments} rowKey={(_, i) => String(i)} height="auto" columns={[
      { key: 'type', header: 'Type', width: '160px', render: (k) => k.type },
      { key: 'id', header: 'Id', width: 'minmax(200px, 2fr)', render: (k) => k.id ?? '—' },
      { key: 'term', header: 'Term', width: '100px', render: (k) => k.term ?? '—' },
      { key: 'pay', header: 'Payment', width: '130px', render: (k) => k.payment ?? '—' },
      { key: 'amount', header: 'Amount', width: '120px', align: 'right', render: (k) => money(k.amount) },
      { key: 'charges', header: 'Charges', width: 'minmax(160px, 2fr)', render: (k) => k.charges.join(', ') },
    ]} />
  )
}

function RunsTab({ view }: { view: DocumentView }) {
  if (!view.runs.length) return <Empty>No runs.</Empty>
  return (
    <DataTable rows={view.runs} rowKey={(r) => String(r.id)} height="auto" columns={[
      { key: 'when', header: 'When', width: '160px', render: (r) => when(r.ts) },
      { key: 'ms', header: 'Took', width: '90px', align: 'right', render: (r) => <span title={`Last parse ${ms(r.elapsed_ms)}`}>{uploadTime(r.total_ms, r.elapsed_ms)}</span> },
      { key: 'by', header: 'By', width: 'minmax(160px, 1.5fr)', render: (r) => <span className="text-muted-foreground">{r.parsed_by ?? '—'}</span> },
      { key: 'calls', header: 'Model calls', width: '100px', align: 'right', render: (r) => r.model_calls },
      { key: 'cost', header: 'Cost', width: '110px', align: 'right', render: (r) => usd((r.jev_cost_usd ?? 0) + (r.claude_cost_usd ?? 0) + (r.learn_cost_usd ?? 0)) },
      { key: 'job', header: 'Learn job', width: '90px', render: (r) => r.learn_job_id ? <Link className="hover:underline" to={`/jobs/${r.learn_job_id}`}>job {r.learn_job_id}</Link> : '—' },
      { key: 'status', header: 'Status', width: '130px', render: (r) => <StatusPill status={r.status} /> },
      { key: 'complete', header: 'Totals', width: '120px', render: (r) => <CompletenessPill value={r.completeness} /> },
      { key: 'acc', header: 'Accuracy', width: '90px', align: 'right', render: (r) => r.quality?.accuracy != null ? `${(Number(r.quality.accuracy) * 100).toFixed(1)}%` : '—' },
      { key: 'lane', header: 'Read by', width: 'minmax(180px, 2fr)', render: (r) => <span className="text-muted-foreground">{r.lane}</span> },
      { key: 'version', header: 'Pipeline', width: '160px', render: (r) => <span className="text-xs text-muted-foreground">{r.version}</span> },
    ]} />
  )
}

function HowTab({ view }: { view: DocumentView }) {
  const spec = view.improved_spec ?? view.learned_spec
  return (
    <div className="space-y-5">
      <div className="text-sm">
        <span className="font-medium">Decision models:</span> {view.decider ?? '—'} · <span className="font-medium">pipeline</span> {view.version}
        {Object.keys(view.cost_usd).length > 0 && <> · <span className="font-medium">cost</span> {usd(view.cost_usd.total)}</>}
      </div>
      {spec && (
        <div className="text-sm">
          A layout spec was {view.improved_spec ? 'improved' : 'learned'} for this file:{' '}
          <Link className="font-medium hover:underline" to={`/specs/${String(spec.id)}`}>{String(spec.id)}</Link> (version {String(spec.version)}, {String(spec.status)})
        </div>
      )}
      {view.decider_errors.length > 0 && <ErrorBox error={view.decider_errors.join('\n')} />}
      {Object.keys(view.timings_ms).length > 0 && (
        <Section title="Time by step">
          <div className="flex flex-wrap gap-2">
            {Object.entries(view.timings_ms).map(([k, t]) => <Stat key={k} label={k} value={ms(t)} />)}
          </div>
        </Section>
      )}
      {view.tables.length > 0 && (
        <Section title="Tables">
          {view.tables.map((t, i) => <JsonBlock key={i} value={t} summary={`Table '${String(t.table)}' — ${String(t.lane)} (header row ${String(t.header_row)})`} />)}
        </Section>
      )}
      {view.llm_calls.length > 0 && (
        <Section title={`Model calls (${view.llm_calls.length})`}>
          <JsonBlock value={view.llm_calls} summary="Every call, with its questions and answers" />
        </Section>
      )}
    </div>
  )
}
