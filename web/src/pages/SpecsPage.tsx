import { Link } from 'react-router'
import { toast } from 'sonner'

import { type SpecSummary, useSetSpecStatus, useSpecs } from '@/api/queries'
import { SpecPill } from '@/components/app/badges'
import { DataTable } from '@/components/app/DataTable'
import { Empty, ErrorBox, Loading, NativeSelect, PageHeader } from '@/components/app/common'
import { when } from '@/lib/format'

export function SpecsPage() {
  const specs = useSpecs()
  const setStatus = useSetSpecStatus()
  const change = (s: SpecSummary, status: SpecSummary['status']) =>
    setStatus.mutate({ id: s.id, status }, { onSuccess: () => toast.success(`${s.id} is now ${status}`), onError: (e) => toast.error(e.message) })
  return (
    <>
      <PageHeader
        title="Layout specs"
        description="How each file layout is read. The agent writes a spec as a draft; a draft becomes trusted after 3 files pass every check with it. A disabled spec is kept but never used."
      />
      {specs.isLoading ? <Loading /> : specs.error ? <ErrorBox error={specs.error} /> : !specs.data?.length ? <Empty>No layout spec yet.</Empty> : (
        <DataTable rows={specs.data} rowKey={(s) => s.id} height="auto" columns={[
          { key: 'id', header: 'Id', width: 'minmax(220px, 2fr)', render: (s) => <Link className="font-mono text-xs hover:underline" to={`/specs/${s.id}`}>{s.id}</Link> },
          { key: 'name', header: 'Name', width: 'minmax(260px, 3fr)', render: (s) => s.name },
          { key: 'status', header: 'Status', width: '100px', render: (s) => <SpecPill status={s.status} /> },
          { key: 'engine', header: 'Engine', width: '130px', render: (s) => s.engine },
          { key: 'version', header: 'Version', width: '80px', align: 'right', render: (s) => s.version },
          { key: 'verified', header: 'Files passed', width: '100px', align: 'right', render: (s) => s.verified },
          { key: 'priority', header: 'Priority', width: '80px', align: 'right', render: (s) => s.priority },
          { key: 'updated', header: 'Updated', width: '150px', render: (s) => when(s.updated_at) },
          {
            key: 'set', header: 'Set status', width: '130px',
            render: (s) => (
              <NativeSelect className="h-7" value={s.status} onChange={(e) => change(s, e.target.value as SpecSummary['status'])} aria-label={`Status of ${s.id}`}>
                {['draft', 'trusted', 'disabled'].map((x) => <option key={x} value={x}>{x}</option>)}
              </NativeSelect>
            ),
          },
        ]} />
      )}
    </>
  )
}
