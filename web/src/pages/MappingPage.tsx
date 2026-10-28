import { useDeferredValue, useState } from 'react'
import { toast } from 'sonner'
import { Trash2Icon } from 'lucide-react'

import { useAddAlias, useAliases, useDeleteAlias, useDeleteHeader, useHeaderFields, useHeaders, useMapping, useResolve, useTeachHeader } from '@/api/queries'
import { DataTable } from '@/components/app/DataTable'
import { Empty, Loading, NativeSelect, PageHeader } from '@/components/app/common'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { when } from '@/lib/format'

export function MappingPage() {
  return (
    <>
      <PageHeader
        title="Service mapping"
        description="A printed service label resolves to a code in this order: a code printed on the document, an alias taught in review, a layout spec's names, an exact name or alias in the mapping file, then a choice among close candidates. Names shared by several codes never resolve on their own."
      />
      <Tabs defaultValue="try">
        <TabsList>
          <TabsTrigger value="try">Try a label</TabsTrigger>
          <TabsTrigger value="aliases">Learned aliases</TabsTrigger>
          <TabsTrigger value="headers">Column headers</TabsTrigger>
          <TabsTrigger value="flagged">Rows to check</TabsTrigger>
          <TabsTrigger value="ambiguous">Shared names</TabsTrigger>
          <TabsTrigger value="all">Full mapping</TabsTrigger>
        </TabsList>
        <TabsContent value="try" className="pt-3"><TryLabel /></TabsContent>
        <TabsContent value="aliases" className="pt-3"><Aliases /></TabsContent>
        <TabsContent value="headers" className="pt-3"><ColumnHeaders /></TabsContent>
        <TabsContent value="flagged" className="pt-3"><MappingTable view="flagged" /></TabsContent>
        <TabsContent value="ambiguous" className="pt-3"><MappingTable view="ambiguous" /></TabsContent>
        <TabsContent value="all" className="pt-3"><MappingTable view="all" /></TabsContent>
      </Tabs>
    </>
  )
}

function TryLabel() {
  const [text, setText] = useState('Savings Plans for AWS Compute usage')
  const [provider, setProvider] = useState('')
  const labelText = useDeferredValue(text)
  const r = useResolve(labelText, provider)
  return (
    <div className="max-w-3xl space-y-3">
      <div className="flex flex-wrap gap-2">
        <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Label as printed on a bill" className="min-w-72 flex-1" />
        <NativeSelect value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">any provider</option>
          {['aws', 'azure', 'gcp'].map((p) => <option key={p} value={p}>{p}</option>)}
        </NativeSelect>
      </div>
      {r.isFetching && !r.data ? <Loading /> : r.data && (
        r.data.code ? (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
            <span className="font-mono font-medium">{r.data.code}</span> — {r.data.name} <span className="text-muted-foreground">(matched by {r.data.how})</span>
          </div>
        ) : (
          <div className="space-y-2">
            <div className={`rounded-lg border p-3 text-sm ${r.data.how === 'ambiguous' ? 'border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40' : 'border-red-200 bg-red-50 dark:border-red-900 dark:bg-red-950/40'}`}>
              {r.data.how === 'ambiguous' ? 'Shared by several codes; a person or a printed code decides.' : 'No match.'}
            </div>
            {r.data.candidates.length > 0 && (
              <ul className="space-y-1 text-sm">
                {r.data.candidates.map(([code, name]) => <li key={code}><span className="font-mono">{code}</span> — {name}</li>)}
              </ul>
            )}
          </div>
        )
      )}
    </div>
  )
}

function Aliases() {
  const aliases = useAliases()
  const add = useAddAlias()
  const del = useDeleteAlias()
  const [provider, setProvider] = useState<'aws' | 'azure' | 'gcp'>('aws')
  const [labelText, setLabel] = useState('')
  const [code, setCode] = useState('')
  return (
    <div className="space-y-4">
      <form
        className="flex flex-wrap items-end gap-2 rounded-lg border p-3"
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate({ provider, label: labelText, code }, {
            onSuccess: () => { toast.success(`Saved: ${labelText} → ${code}`); setLabel(''); setCode('') },
            onError: (er) => toast.error(er.message),
          })
        }}
      >
        <label className="space-y-1 text-xs text-muted-foreground">Provider
          <NativeSelect className="block" value={provider} onChange={(e) => setProvider(e.target.value as typeof provider)}>
            {['aws', 'azure', 'gcp'].map((p) => <option key={p} value={p}>{p}</option>)}
          </NativeSelect>
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">Printed label
          <Input value={labelText} onChange={(e) => setLabel(e.target.value)} className="w-72" required />
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">Code
          <Input value={code} onChange={(e) => setCode(e.target.value)} className="w-48 font-mono" required />
        </label>
        <Button type="submit" disabled={add.isPending}>Add an alias</Button>
      </form>
      {aliases.isLoading ? <Loading /> : !aliases.data?.length ? <Empty>None yet. Setting a service code in review saves the label here.</Empty> : (
        <DataTable rows={aliases.data} rowKey={(a) => `${a.provider}|${a.label_norm}`} height="auto" columns={[
          { key: 'provider', header: 'Provider', width: '90px', render: (a) => a.provider },
          { key: 'label', header: 'Label', width: 'minmax(240px, 3fr)', render: (a) => a.label },
          { key: 'code', header: 'Code', width: '180px', render: (a) => <span className="font-mono text-xs">{a.code}</span> },
          { key: 'source', header: 'Source', width: 'minmax(160px, 2fr)', render: (a) => <span className="text-muted-foreground">{a.source}</span> },
          { key: 'created', header: 'Added', width: '150px', render: (a) => when(a.created) },
          {
            key: 'del', header: '', width: '44px', align: 'center',
            render: (a) => <button aria-label={`Remove ${a.label}`} className="text-muted-foreground hover:text-red-600" onClick={() => del.mutate(a)}><Trash2Icon className="size-4" /></button>,
          },
        ]} />
      )}
    </div>
  )
}

// headers the rule mapper reads as a field: learned from approved reviews and verified specs, or taught here; a
// rejected one never maps to its field (not by a rule, the seed dictionary or the values either)
function ColumnHeaders() {
  const entries = useHeaders()
  const fields = useHeaderFields()
  const teach = useTeachHeader()
  const del = useDeleteHeader()
  const [header, setHeader] = useState('')
  const [field, setField] = useState('billed_cost')
  const submit = (reject: boolean) =>
    teach.mutate({ header, field, reject }, {
      onSuccess: () => { toast.success(reject ? `Rejected: ${header} ↛ ${field}` : `Saved: ${header} → ${field}`); setHeader('') },
      onError: (er) => toast.error(er.message),
    })
  return (
    <div className="space-y-4">
      <p className="max-w-3xl text-sm text-muted-foreground">
        The rule mapper reads a column by its header: its rules, a seed dictionary of foreign headers, and the headers learned here. A header learned from an approved review or added here counts at once; one learned from verified specs counts after two. Approving a review with no charge changed teaches the headers of the spec that read the file.
      </p>
      <form
        className="flex flex-wrap items-end gap-2 rounded-lg border p-3"
        onSubmit={(e) => { e.preventDefault(); submit(false) }}
      >
        <label className="space-y-1 text-xs text-muted-foreground">Header as printed
          <Input value={header} onChange={(e) => setHeader(e.target.value)} className="w-72" required />
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">Field
          <NativeSelect className="block" value={field} onChange={(e) => setField(e.target.value)}>
            {(fields.data ?? [{ field, description: '' }]).map((f) => <option key={f.field} value={f.field} title={f.description}>{f.field}</option>)}
          </NativeSelect>
        </label>
        <Button type="submit" disabled={teach.isPending || !header.trim()}>Add a header</Button>
        <Button type="button" variant="outline" disabled={teach.isPending || !header.trim()} onClick={() => submit(true)}>Reject for this field</Button>
      </form>
      {entries.isLoading ? <Loading /> : !entries.data?.length ? <Empty>None yet. Approving a review with no charge changed saves the headers of the spec here.</Empty> : (
        <DataTable rows={entries.data} rowKey={(h) => `${h.header_norm}|${h.field}`} height="auto" columns={[
          { key: 'header', header: 'Header', width: 'minmax(220px, 3fr)', render: (h) => h.header_norm },
          { key: 'field', header: 'Field', width: '200px', render: (h) => <span className="font-mono text-xs">{h.field}</span> },
          { key: 'state', header: 'State', width: '110px', render: (h) => h.rejected ? <span className="text-red-700 dark:text-red-300">rejected</span> : 'maps' },
          { key: 'count', header: 'Confirmed', width: '100px', align: 'right', render: (h) => h.count },
          { key: 'source', header: 'Source', width: '100px', render: (h) => <span className="text-muted-foreground">{h.source}</span> },
          { key: 'updated', header: 'Updated', width: '150px', render: (h) => when(h.updated) },
          { key: 'by', header: 'By', width: 'minmax(120px, 1fr)', render: (h) => <span className="text-muted-foreground">{h.updated_by ?? ''}</span> },
          {
            key: 'del', header: '', width: '44px', align: 'center',
            render: (h) => <button aria-label={`Remove ${h.header_norm}`} className="text-muted-foreground hover:text-red-600" onClick={() => del.mutate(h)}><Trash2Icon className="size-4" /></button>,
          },
        ]} />
      )}
    </div>
  )
}

function MappingTable({ view }: { view: 'all' | 'flagged' | 'ambiguous' }) {
  const [q, setQ] = useState('')
  const [provider, setProvider] = useState('')
  const [offset, setOffset] = useState(0)
  const query = useDeferredValue(q)
  const m = useMapping(query, provider, view, offset)
  const total = m.data?.total ?? 0
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input placeholder="Search code, name or alias" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0) }} className="w-72" />
        <NativeSelect value={provider} onChange={(e) => { setProvider(e.target.value); setOffset(0) }}>
          <option value="">all providers</option>
          {['aws', 'azure', 'gcp'].map((p) => <option key={p} value={p}>{p}</option>)}
        </NativeSelect>
        <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 200))}>Previous</Button>
        <span className="text-sm text-muted-foreground tabular-nums">{total ? `${offset + 1}–${Math.min(total, offset + 200)} of ${total.toLocaleString()}` : '0 rows'}</span>
        <Button size="sm" variant="outline" disabled={offset + 200 >= total} onClick={() => setOffset(offset + 200)}>Next</Button>
      </div>
      <DataTable rows={m.data?.rows ?? []} rowKey={(r) => `${r.provider}|${r.code}`} height={560} empty={m.isLoading ? 'Loading…' : 'No row matches.'} columns={[
        { key: 'provider', header: 'Provider', width: '80px', render: (r) => r.provider },
        { key: 'code', header: 'Code', width: '200px', render: (r) => <span className="font-mono text-xs">{r.code}</span> },
        { key: 'name', header: 'Name', width: 'minmax(220px, 2fr)', render: (r) => r.name },
        { key: 'match', header: 'Match', width: '100px', render: (r) => r.match },
        { key: 'aliases', header: 'Aliases', width: 'minmax(200px, 2fr)', render: (r) => <span className="text-muted-foreground">{r.aliases.join(', ')}</span> },
        { key: 'note', header: 'Note', width: 'minmax(240px, 3fr)', render: (r) => <span className="text-muted-foreground" title={r.note}>{r.was ? `was “${r.was}”. ` : ''}{r.note}</span> },
      ]} />
    </div>
  )
}
