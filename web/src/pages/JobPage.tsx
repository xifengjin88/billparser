import { Link, useParams } from 'react-router'
import { toast } from 'sonner'

import { type TraceEvent, useJob, useJobTrace, useRetryJob, useStopJob } from '@/api/queries'
import { JobPill, SpecPill } from '@/components/app/badges'
import { Empty, ErrorBox, JsonBlock, Loading, PageHeader } from '@/components/app/common'
import { jobKind } from '@/pages/JobsPage'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { took, usd, when } from '@/lib/format'
import { cn } from '@/lib/utils'

type Block = { type: string; text?: string; name?: string; id?: string; input?: Record<string, unknown> }

export function JobPage() {
  const id = Number(useParams().id)
  const job = useJob(id)
  const running = job.data?.status === 'running'
  const trace = useJobTrace(id, running)
  const retry = useRetryJob()
  const stop = useStopJob()
  if (job.isLoading) return <Loading />
  if (job.error) return <ErrorBox error={job.error} />
  const j = job.data!
  const events = trace.data ?? []
  const start = events.find((e) => e.event === 'job')
  const finish = events.find((e) => e.event === 'finish')
  const results = new Map(events.filter((e) => e.event === 'tool').map((e) => [String(e.id), e]))
  const rounds = Array.from(new Set(events.filter((e) => e.event !== 'job' && typeof e.round === 'number').map((e) => e.round as number))).sort((a, b) => a - b)
  return (
    <>
      <PageHeader
        title={<>Job {j.id} · {j.name}</>}
        description={
          <span className="flex flex-wrap items-center gap-1.5">
            {jobKind(j.layout_key)} · <JobPill status={j.status} /> · spec{' '}
            {j.spec_id ? <Link className="hover:underline" to={`/specs/${j.spec_id}`}>{j.spec_id}</Link> : '—'} {j.spec_status && <SpecPill status={j.spec_status} />}
            · {usd(j.cost_usd)} · {running ? `running for ${took(Date.now() / 1000 - (j.started ?? j.created))}` : `took ${took(j.seconds)}`} · started {when(j.started ?? j.created)}
          </span>
        }
        actions={running ? (
          <Button variant="destructive" size="sm" disabled={stop.isPending}
            title="Ends the run at its next step (a Claude request already sent finishes first). The file keeps the result it had."
            onClick={() => stop.mutate(j.id, { onSuccess: () => toast.success('Stopping: the run ends at its next step.'), onError: (e) => toast.error(e.message) })}>
            Stop this run
          </Button>
        ) : (
          <Button variant="outline" size="sm" disabled={retry.isPending}
            onClick={() => retry.mutate(j.id, { onSuccess: () => toast.success('Queued: the next upload of the file runs it again (costs money).'), onError: (e) => toast.error(e.message) })}>
            Retry this job
          </Button>
        )}
      />
      {j.error && <div className="mb-4"><ErrorBox error={j.error} /></div>}
      {trace.isLoading ? <Loading what="Reading the trace" /> : !events.length ? (
        <Empty>No trace for this job: it ran before traces were kept, or it used the sandbox.</Empty>
      ) : (
        <div className="space-y-4">
          <div className="text-sm">
            <span className="font-medium">Outcome:</span>{' '}
            {finish ? `${String(finish.outcome)} after ${String(finish.rounds)} round(s), about ${usd(Number(finish.cost_usd))}` : 'none recorded (the run is active, or it stopped before the end).'}
          </div>
          {start && <JsonBlock value={{ harness: start.harness, effort: start.effort, limits: start.limits, examples: start.examples, start_spec: start.start_spec }} summary="Job settings" />}
          {rounds.map((n) => <Round key={n} n={n} last={n === rounds[rounds.length - 1]} events={events.filter((e) => e.round === n && e.event !== 'job')} results={results} />)}
        </div>
      )}
    </>
  )
}

function Round({ n, last, events, results }: { n: number; last: boolean; events: TraceEvent[]; results: Map<string, TraceEvent> }) {
  const tools = events.filter((e) => e.event === 'tool')
  const checks = tools.filter((e) => e.name === 'check_spec').length
  const cost = events.filter((e) => e.event === 'response').reduce((s, e) => s + (Number(e.cost_usd) || 0), 0)
  return (
    <Collapsible defaultOpen={last} className="rounded-lg border">
      <CollapsibleTrigger className="w-full px-3 py-2 text-left text-sm font-medium hover:bg-muted/50">
        Round {n}: {tools.length} tool call(s), {checks} check(s), about {usd(cost)}
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-2 border-t px-3 py-3">
        {events.map((e, i) => <Step key={i} e={e} results={results} />)}
      </CollapsibleContent>
    </Collapsible>
  )
}

function Step({ e, results }: { e: TraceEvent; results: Map<string, TraceEvent> }) {
  switch (e.event) {
    case 'round':
      return <JsonBlock value={e.task} summary="The task Claude got" />
    case 'response': {
      const usage = (e.usage ?? {}) as Record<string, number>
      return (
        <div className="space-y-1.5">
          <div className="text-xs text-muted-foreground">
            {when(e.ts)} · Claude · {String(e.ms ?? 0)} ms · stop: {String(e.stop_reason)} · tokens in {usage.input_tokens ?? '—'} / out {usage.output_tokens ?? '—'} · {usd(Number(e.cost_usd) || 0)}
          </div>
          {((e.blocks ?? []) as Block[]).map((b, i) => <BlockView key={i} b={b} results={results} />)}
        </div>
      )
    }
    case 'author':
      return <div className="text-sm"><span className="font-medium">Author's answer:</span> {String(e.note ?? '—')}</div>
    case 'verify': {
      const failed = ((e.gates ?? []) as { gate: string; ok: boolean }[]).filter((g) => !g.ok).map((g) => g.gate)
      return <div className="text-sm"><span className="font-medium">Loop verify:</span> {e.ok ? 'passed' : `failed ${failed.join(', ')}`}</div>
    }
    case 'review': {
      const findings = (e.findings ?? []) as { check: string }[]
      return <div className="text-sm"><span className="font-medium">Loop review:</span> {String(e.verdict)}{findings.length ? ` — ${findings.map((f) => f.check).join(', ')}` : ''}</div>
    }
    case 'apply':
      return <JsonBlock value={e.suggest} summary="Exact fix applied by the review" />
    default:
      return null
  }
}

function BlockView({ b, results }: { b: Block; results: Map<string, TraceEvent> }) {
  if (b.type === 'thinking' && b.text) return <blockquote className="border-l-2 pl-3 text-sm whitespace-pre-wrap text-muted-foreground">{b.text}</blockquote>
  if (b.type === 'text' && b.text) return <p className="text-sm whitespace-pre-wrap">{b.text}</p>
  if (b.type !== 'tool_use') return null
  const { spec, ...args } = (b.input ?? {}) as Record<string, unknown>
  const r = b.id ? results.get(b.id) : undefined
  const first = r ? String(r.result ?? '').split('\n')[0] : ''
  const failed = !!r && (r.error === true || first.startsWith('FAILED'))
  const passed = first.startsWith('PASSED')
  return (
    <div className="rounded-md border bg-muted/20 p-2 text-sm">
      <div className="font-mono text-xs">🔧 {b.name} {Object.keys(args).length ? JSON.stringify(args) : ''}</div>
      {spec !== undefined && <div className="mt-1"><JsonBlock value={spec} summary="The spec" /></div>}
      {r && (
        <details className="mt-1">
          <summary className={cn('cursor-pointer text-xs', failed ? 'text-red-700 dark:text-red-400' : passed ? 'text-emerald-700 dark:text-emerald-400' : 'text-muted-foreground')}>
            {failed ? '✗' : passed ? '✓' : '↳'} {first.slice(0, 140)}
          </summary>
          <pre className="mt-1 max-h-96 overflow-auto font-mono text-xs whitespace-pre-wrap">{String(r.result ?? '')}</pre>
        </details>
      )}
    </div>
  )
}

