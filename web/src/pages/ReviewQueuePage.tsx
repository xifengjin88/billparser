import { Link, useNavigate } from 'react-router'

import { useDocuments, useReviewQueue } from '@/api/queries'
import { CompletenessPill } from '@/components/app/badges'
import { DataTable } from '@/components/app/DataTable'
import { Empty, ErrorBox, Loading, NativeSelect, PageHeader, Section } from '@/components/app/common'
import { money, when } from '@/lib/format'

export function ReviewQueuePage() {
  const queue = useReviewQueue()
  const docs = useDocuments()
  const navigate = useNavigate()
  const approved = docs.data?.filter((d) => d.status === 'approved').length ?? 0
  return (
    <>
      <PageHeader
        title="Review"
        description="Start with the gaps, fix the charge table, check the totals again, work through the checklist, then approve. Approving saves the document to the gold set, trusts its layout's template and remembers the service codes you set."
        actions={<span className="text-sm text-muted-foreground">Gold set: {approved} approved</span>}
      />
      <Section title="Waiting for review, most money at risk first">
        {queue.isLoading ? <Loading /> : queue.error ? <ErrorBox error={queue.error} /> : queue.data?.length ? (
          <DataTable rows={queue.data} rowKey={(d) => d.sha256} height="auto" onRowClick={(d) => navigate(`/review/${d.sha256}`)} columns={[
            { key: 'name', header: 'File', width: 'minmax(260px, 3fr)', render: (d) => <Link className="hover:underline" to={`/review/${d.sha256}`}>{d.name}</Link> },
            { key: 'risk', header: 'Money at risk', width: '140px', align: 'right', render: (d) => money(d.money_at_risk) },
            { key: 'completeness', header: 'Totals', width: '120px', render: (d) => <CompletenessPill value={d.completeness} /> },
            { key: 'lane', header: 'Read by', width: 'minmax(180px, 2fr)', render: (d) => <span className="text-muted-foreground">{d.lane}</span> },
            { key: 'when', header: 'Parsed', width: '150px', render: (d) => when(d.uploaded_at) },
          ]} />
        ) : <Empty>Nothing waiting for review.</Empty>}
      </Section>
      <Section title="Review any document" className="mt-8">
        <NativeSelect className="w-full max-w-xl" value="" onChange={(e) => e.target.value && navigate(`/review/${e.target.value}`)}>
          <option value="">Choose a document…</option>
          {docs.data?.filter((d) => d.status !== 'processing').map((d) => <option key={d.sha256} value={d.sha256}>{d.name} · {d.status}</option>)}
        </NativeSelect>
      </Section>
    </>
  )
}
