import { useState } from 'react'
import { useParams } from 'react-router'

import { useSpec, useSpecHistory } from '@/api/queries'
import { SpecPill } from '@/components/app/badges'
import { ErrorBox, JsonBlock, Loading, PageHeader, Section } from '@/components/app/common'
import { when } from '@/lib/format'
import { cn } from '@/lib/utils'

export function SpecPage() {
  const { id = '' } = useParams()
  const spec = useSpec(id)
  const history = useSpecHistory(id)
  const [version, setVersion] = useState<number | null>(null)
  if (spec.isLoading) return <Loading />
  if (spec.error) return <ErrorBox error={spec.error} />
  const s = spec.data!
  const shown = version === null ? null : history.data?.find((h) => h.version === version)
  return (
    <>
      <PageHeader
        title={String(s.name ?? id)}
        description={<span className="flex flex-wrap items-center gap-1.5"><span className="font-mono text-xs">{id}</span> <SpecPill status={String(s.status)} /> · version {String(s.version ?? 1)} · engine {String(s.engine)}</span>}
      />
      {typeof s.notes === 'string' && s.notes && <p className="mb-4 max-w-4xl text-sm text-muted-foreground">{s.notes}</p>}
      <div className="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
        <Section title="Versions">
          <div className="flex flex-col gap-1">
            <button className={cn('rounded-md px-2 py-1 text-left text-sm hover:bg-muted', version === null && 'bg-muted font-medium')} onClick={() => setVersion(null)}>Current</button>
            {(history.data ?? []).slice().reverse().map((h) => (
              <button key={h.version} className={cn('rounded-md px-2 py-1 text-left text-sm hover:bg-muted', version === h.version && 'bg-muted font-medium')} onClick={() => setVersion(h.version)}>
                v{h.version} <span className="text-xs text-muted-foreground">{when(h.saved_at)}</span>
              </button>
            ))}
          </div>
        </Section>
        <div className="min-w-0">
          <JsonBlock open value={shown ? shown.spec : s} summary={shown ? `Version ${shown.version}` : 'Current spec'} />
        </div>
      </div>
    </>
  )
}
