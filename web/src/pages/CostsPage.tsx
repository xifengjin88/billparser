import { Link } from 'react-router'

import { useCosts } from '@/api/queries'
import { JobPill, StatusPill } from '@/components/app/badges'
import { DataTable } from '@/components/app/DataTable'
import { Empty, ErrorBox, JsonBlock, Loading, PageHeader, Section, Stat } from '@/components/app/common'
import { jobKind } from '@/pages/JobsPage'
import { took, usd, when } from '@/lib/format'

type Run = { id: number; ts: number; name: string; jev_cost_usd: number | null; claude_cost_usd: number | null; learn_cost_usd: number | null; model_calls: number; lane: string; status: string; learn_job_id: number | null; sha256: string }
type Job = { id: number; layout_key: string; name: string; status: string; spec_id: string | null; cost_usd: number | null; seconds: number | null; attempts: number; updated: number }

export function CostsPage() {
  const costs = useCosts()
  if (costs.isLoading) return <Loading />
  if (costs.error) return <ErrorBox error={costs.error} />
  const c = costs.data!
  const runs = c.runs as unknown as Run[]
  const jobs = c.jobs as unknown as Job[]
  return (
    <>
      <PageHeader title="Costs" description="What each parse cost: the Jev and Claude decision calls of the parse, and the agent run when an upload learned or improved a layout spec. Answers from the cache cost nothing." />
      <div className="mb-6 grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Stat label="Total" value={usd(c.totals.total)} />
        <Stat label="Jev" value={usd(c.totals.jev)} />
        <Stat label="Claude (decisions)" value={usd(c.totals.claude)} />
        <Stat label="Learning jobs" value={usd(c.totals.learning)} hint={`${jobs.length} job(s)`} />
      </div>
      {!runs.length && !jobs.length ? <Empty>Nothing parsed yet. Every upload adds a run here with its cost.</Empty> : (
        <div className="space-y-6">
          <Section title={`Runs (${runs.length})`}>
            <DataTable rows={runs} rowKey={(r) => String(r.id)} height="auto" columns={[
              { key: 'when', header: 'When', width: '150px', render: (r) => when(r.ts) },
              { key: 'file', header: 'File', width: 'minmax(220px, 3fr)', render: (r) => <Link className="hover:underline" to={`/documents/${r.sha256}`}>{r.name}</Link> },
              { key: 'jev', header: 'Jev', width: '100px', align: 'right', render: (r) => usd(r.jev_cost_usd) },
              { key: 'claude', header: 'Claude', width: '100px', align: 'right', render: (r) => usd(r.claude_cost_usd) },
              { key: 'learn', header: 'Learning', width: '100px', align: 'right', render: (r) => usd(r.learn_cost_usd) },
              { key: 'total', header: 'Total', width: '100px', align: 'right', render: (r) => usd((r.jev_cost_usd ?? 0) + (r.claude_cost_usd ?? 0) + (r.learn_cost_usd ?? 0)) },
              { key: 'calls', header: 'Calls', width: '70px', align: 'right', render: (r) => r.model_calls },
              { key: 'status', header: 'Status', width: '130px', render: (r) => <StatusPill status={r.status} /> },
              { key: 'job', header: 'Job', width: '80px', render: (r) => r.learn_job_id ? <Link className="hover:underline" to={`/jobs/${r.learn_job_id}`}>{r.learn_job_id}</Link> : '—' },
            ]} />
          </Section>
          {jobs.length > 0 && (
            <Section title={`Learning jobs (${jobs.length})`}>
              <DataTable rows={jobs} rowKey={(j) => String(j.id)} height="auto" columns={[
                { key: 'id', header: 'Job', width: '70px', render: (j) => <Link className="hover:underline" to={`/jobs/${j.id}`}>{j.id}</Link> },
                { key: 'kind', header: 'Kind', width: '80px', render: (j) => jobKind(j.layout_key) },
                { key: 'file', header: 'File', width: 'minmax(220px, 3fr)', render: (j) => j.name },
                { key: 'status', header: 'Status', width: '100px', render: (j) => <JobPill status={j.status} /> },
                { key: 'spec', header: 'Spec', width: 'minmax(160px, 2fr)', render: (j) => j.spec_id ?? '—' },
                { key: 'cost', header: 'Cost', width: '100px', align: 'right', render: (j) => usd(j.cost_usd) },
                { key: 'took', header: 'Took', width: '90px', align: 'right', render: (j) => took(j.seconds) },
                { key: 'updated', header: 'Updated', width: '150px', render: (j) => when(j.updated) },
              ]} />
            </Section>
          )}
        </div>
      )}
      <div className="mt-6"><JsonBlock value={c.prices} summary="Prices used (USD per million tokens)" /></div>
    </>
  )
}
