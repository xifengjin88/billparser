import { useMemo, useState } from 'react'
import { XIcon } from 'lucide-react'

import type { ChargeRow, DocumentView } from '@/api/client'
import { useChargeDetail } from '@/api/queries'
import { DataTable, type Column } from '@/components/app/DataTable'
import { ErrorBox, JsonBlock, Loading, NativeSelect } from '@/components/app/common'
import { SourceView } from '@/components/app/source'
import { useColumnFilters } from '@/components/app/tableFilters'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { money } from '@/lib/format'

export type ChargeFilter = { service: string; text: string; noCode: boolean; withGap: boolean }

export function useChargeFilter(charges: ChargeRow[]) {
  const [f, setF] = useState<ChargeFilter>({ service: '', text: '', noCode: false, withGap: false })
  const services = useMemo(
    () => Array.from(new Set(charges.map((c) => c.service).filter((s): s is string => !!s))).sort(),
    [charges],
  )
  const shown = useMemo(() => {
    const t = f.text.trim().toLowerCase()
    return charges.filter((c) =>
      (!f.service || c.service === f.service) &&
      (!t || c.description.toLowerCase().includes(t) || (c.service_code ?? '').toLowerCase().includes(t)) &&
      (!f.noCode || !c.service_code) &&
      (!f.withGap || c.has_gap))
  }, [charges, f])
  return { f, setF, services, shown }
}

export function ChargeFilters({ f, setF, services, shown, total, count, onClear }: ReturnType<typeof useChargeFilter> & {
  total: number
  /** The rows shown after the column filters too (else the quick filters' count). */
  count?: number
  onClear?: () => void
}) {
  const quick = !!(f.service || f.text || f.noCode || f.withGap)
  return (
    <div className="flex flex-wrap items-center gap-2">
      {services.length > 1 && (
        <NativeSelect value={f.service} onChange={(e) => setF({ ...f, service: e.target.value })} className="max-w-64">
          <option value="">All services ({services.length})</option>
          {services.map((s) => <option key={s} value={s}>{s}</option>)}
        </NativeSelect>
      )}
      <Input placeholder="Description or code contains" value={f.text} onChange={(e) => setF({ ...f, text: e.target.value })} className="w-64" />
      <label className="flex items-center gap-1.5 text-sm"><Checkbox checked={f.noCode} onCheckedChange={(v) => setF({ ...f, noCode: !!v })} /> No service code</label>
      <label className="flex items-center gap-1.5 text-sm"><Checkbox checked={f.withGap} onCheckedChange={(v) => setF({ ...f, withGap: !!v })} /> A gap points at it</label>
      <span className="text-sm text-muted-foreground tabular-nums">Showing {(count ?? shown.length).toLocaleString()} of {total.toLocaleString()} charges</span>
      {onClear && (quick || (count ?? shown.length) !== shown.length) && (
        <Button variant="ghost" size="sm" onClick={() => { setF({ service: '', text: '', noCode: false, withGap: false }); onClear() }}>
          <XIcon /> Clear filters
        </Button>
      )}
    </div>
  )
}

const opt = (v: string | number | null | undefined) => (v === null || v === undefined || v === '' ? <span className="text-muted-foreground/60">—</span> : v)

export const CHARGE_COLUMNS: Column<ChargeRow>[] = [
  { key: 'description', header: 'Description', value: (c) => c.description, filter: 'text', width: 'minmax(260px, 3fr)', render: (c) => <span title={c.description}>{c.description}</span> },
  { key: 'cost', header: 'Cost', value: (c) => c.cost, filter: 'number', width: '120px', align: 'right', render: (c) => <span className={c.counted ? '' : 'text-muted-foreground line-through'}>{money(c.cost_exact)}</span> },
  { key: 'kind', header: 'Kind', value: (c) => c.kind, width: '140px', render: (c) => <span title={c.kind_by}>{c.kind.replaceAll('_', ' ')}</span> },
  { key: 'coverage', header: 'Coverage', value: (c) => c.coverage, width: '120px', render: (c) => opt(c.coverage?.replaceAll('_', ' ')) },
  { key: 'priceMatch', header: 'Price match', value: (c) => c.price_match_level, width: '120px', render: (c) => opt(c.price_match_level?.replaceAll('_', ' ')) },
  { key: 'list', header: 'List cost', value: (c) => c.list_cost, filter: 'number', width: '110px', align: 'right', render: (c) => c.list_cost == null ? opt(null) : money(c.list_cost) },
  { key: 'contracted', header: 'Contracted cost', value: (c) => c.contracted_cost, filter: 'number', width: '130px', align: 'right', render: (c) => c.contracted_cost == null ? opt(null) : money(c.contracted_cost) },
  { key: 'effective', header: 'Effective cost', value: (c) => c.effective_cost, filter: 'number', width: '120px', align: 'right', render: (c) => c.effective_cost == null ? opt(null) : money(c.effective_cost) },
  { key: 'service', header: 'Service', value: (c) => c.service, width: 'minmax(160px, 1.5fr)', render: (c) => opt(c.service) },
  { key: 'code', header: 'Service code', value: (c) => c.service_code, width: '150px', render: (c) => c.service_code ? <span className="font-mono text-xs">{c.service_code}</span> : <span className="text-amber-700 dark:text-amber-400">none</span> },
  { key: 'region', header: 'Region', value: (c) => c.region, width: '110px', render: (c) => opt(c.region) },
  { key: 'type', header: 'Instance', value: (c) => c.instance_type, width: '120px', render: (c) => opt(c.instance_type) },
  { key: 'os', header: 'OS', value: (c) => c.operating_system, width: '100px', render: (c) => opt(c.operating_system) },
  { key: 'db', header: 'DB engine', value: (c) => c.database_engine, width: '110px', render: (c) => opt(c.database_engine) },
  { key: 'dep', header: 'Deployment', value: (c) => c.deployment_option, width: '110px', render: (c) => opt(c.deployment_option) },
  { key: 'qty', header: 'Qty', value: (c) => c.quantity, filter: 'number', width: '90px', align: 'right', render: (c) => opt(c.quantity?.toLocaleString()) },
  { key: 'price', header: 'Unit price', value: (c) => c.unit_price, filter: 'number', width: '100px', align: 'right', render: (c) => opt(c.unit_price?.toLocaleString()) },
  { key: 'listUnit', header: 'List unit price', value: (c) => c.list_unit_price, filter: 'number', width: '120px', align: 'right', render: (c) => opt(c.list_unit_price?.toLocaleString()) },
  { key: 'contractedUnit', header: 'Contracted unit price', value: (c) => c.contracted_unit_price, filter: 'number', width: '150px', align: 'right', render: (c) => opt(c.contracted_unit_price?.toLocaleString()) },
  { key: 'usageType', header: 'Usage type', value: (c) => c.usage_type, width: '200px', render: (c) => opt(c.usage_type) },
  { key: 'operation', header: 'Operation', value: (c) => c.operation, width: '150px', render: (c) => opt(c.operation) },
  { key: 'sku', header: 'SKU', value: (c) => c.sku_id, width: '170px', render: (c) => c.sku_id ? <span className="font-mono text-xs">{c.sku_id}</span> : opt(null) },
  { key: 'resource', header: 'Resource', value: (c) => c.resource_id, width: '220px', render: (c) => c.resource_id ? <span className="font-mono text-xs" title={c.resource_id}>{c.resource_id}</span> : opt(null) },
  { key: 'commitment', header: 'Commitment', value: (c) => c.commitment_id, width: '220px', render: (c) => c.commitment_id ? <span className="font-mono text-xs" title={c.commitment_id}>{c.commitment_id}</span> : opt(null) },
  { key: 'source', header: 'Source', value: (c) => c.source, filter: 'text', width: '200px', render: (c) => <span className="text-xs text-muted-foreground">{c.source}</span> },
]

export function ChargesTab({ view }: { view: DocumentView }) {
  const filter = useChargeFilter(view.charges)
  const columns = useColumnFilters(filter.shown, CHARGE_COLUMNS)
  const [open, setOpen] = useState<ChargeRow | null>(null)
  return (
    <div className="space-y-3">
      <ChargeFilters {...filter} total={view.charges.length} count={columns.shown.length}
        onClear={columns.clear} />
      <DataTable
        filters={columns}
        rows={columns.shown}
        rowKey={(c) => c.id}
        columns={CHARGE_COLUMNS}
        onRowClick={setOpen}
        selected={open?.id}
        height={600}
        rowClassName={(c) => (c.has_gap ? 'bg-amber-50/60 dark:bg-amber-950/30' : undefined)}
        empty={view.charges.length ? 'No charge matches the filter.' : 'No charges.'}
      />
      <Sheet open={!!open} onOpenChange={(o) => { if (!o) setOpen(null) }}>
        <SheetContent className="w-full overflow-y-auto data-[side=right]:sm:max-w-3xl">
          {open && <ChargeDetailPanel sha={view.sha256} charge={open} />}
        </SheetContent>
      </Sheet>
    </div>
  )
}

function ChargeDetailPanel({ sha, charge }: { sha: string; charge: ChargeRow }) {
  const d = useChargeDetail(sha, charge.id)
  return (
    <>
      <SheetHeader>
        <SheetTitle className="pr-8 break-words">{charge.description}</SheetTitle>
        <SheetDescription>
          {money(charge.cost_exact)} · {charge.kind.replaceAll('_', ' ')}
          {charge.coverage ? ` · ${charge.coverage.replaceAll('_', ' ')}` : ''} · {charge.id}
        </SheetDescription>
      </SheetHeader>
      <div className="space-y-4 px-4 pb-6">
        {d.isLoading ? <Loading what="Reading the charge" /> : d.error ? <ErrorBox error={d.error} /> : d.data && (
          <>
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-xs">
                <thead className="bg-muted text-muted-foreground">
                  <tr>{['Field', 'Value', 'Origin', 'By', 'Where'].map((h) => <th key={h} className="px-2 py-1.5 text-left font-medium">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {d.data.fields.map((f) => (
                    <tr key={f.field} className="border-t">
                      <td className="px-2 py-1 font-medium">{f.field.replaceAll('_', ' ')}</td>
                      <td className="px-2 py-1 break-all">{f.value}</td>
                      <td className="px-2 py-1">{f.origin}</td>
                      <td className="px-2 py-1 text-muted-foreground">{f.source ?? ''}{f.confidence != null ? ` ${f.confidence.toFixed(2)}` : ''}</td>
                      <td className="px-2 py-1 text-muted-foreground">{f.cite.label}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {Object.keys(d.data.attributes).length > 0 && <JsonBlock value={d.data.attributes} summary="Attributes" />}
          </>
        )}
        <SourceView sha={sha} cite={charge.cite} />
      </div>
    </>
  )
}
