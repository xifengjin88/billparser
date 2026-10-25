import { Link, useNavigate } from 'react-router'

import { useJobs } from '@/api/queries'
import { JobPill, SpecPill } from '@/components/app/badges'
import { DataTable } from '@/components/app/DataTable'
import { Empty, ErrorBox, Loading, PageHeader } from '@/components/app/common'
import { took, usd, when } from '@/lib/format'

export const jobKind = (layoutKey: string) => (layoutKey.startsWith('improve:') ? 'improve' : 'learn')

export function JobsPage() {
  const jobs = useJobs()
  const navigate = useNavigate()
  return (
    <>
      <PageHeader title="Learning jobs" description="Every run that learned or improved a layout spec, step by step: what Claude read, its reasoning summary, each spec it checked and the result of each check." />
      {jobs.isLoading ? <Loading /> : jobs.error ? <ErrorBox error={jobs.error} /> : !jobs.data?.length ? (
        <Empty>No learning job yet. A job starts when you upload a file that no layout spec reads (with learning on).</Empty>
      ) : (
        <DataTable rows={jobs.data} rowKey={(j) => String(j.id)} height="auto" onRowClick={(j) => navigate(`/jobs/${j.id}`)} columns={[
          { key: 'id', header: 'Job', width: '70px', render: (j) => <Link className="hover:underline" to={`/jobs/${j.id}`}>{j.id}</Link> },
          { key: 'kind', header: 'Kind', width: '80px', render: (j) => jobKind(j.layout_key) },
          { key: 'file', header: 'File', width: 'minmax(220px, 3fr)', render: (j) => j.name },
          { key: 'status', header: 'Status', width: '100px', render: (j) => <JobPill status={j.status} /> },
          { key: 'spec', header: 'Spec', width: 'minmax(180px, 2fr)', render: (j) => j.spec_id ? <Link className="hover:underline" to={`/specs/${j.spec_id}`}>{j.spec_id}</Link> : '—' },
          { key: 'specstatus', header: 'Spec status', width: '110px', render: (j) => j.spec_status ? <SpecPill status={j.spec_status} /> : '—' },
          { key: 'cost', header: 'Cost', width: '100px', align: 'right', render: (j) => usd(j.cost_usd) },
          { key: 'took', header: 'Took', width: '90px', align: 'right', render: (j) => j.status === 'running' ? 'running' : took(j.seconds) },
          { key: 'attempts', header: 'Tries', width: '60px', align: 'right', render: (j) => j.attempts },
          { key: 'updated', header: 'Updated', width: '150px', render: (j) => when(j.updated) },
          { key: 'error', header: 'Error', width: 'minmax(200px, 3fr)', render: (j) => <span className="text-muted-foreground" title={j.error ?? ''}>{j.error ?? ''}</span> },
        ]} />
      )}
    </>
  )
}
