import { useRef, useState } from 'react'
import { Link } from 'react-router'
import { toast } from 'sonner'
import { FileUpIcon, Loader2Icon, XIcon } from 'lucide-react'

import type { ParseTask } from '@/api/client'
import { learningActive, useDocuments, useSettings, useStartParse, useTasks } from '@/api/queries'
import { CompletenessPill, ConfidencePill, Pill, StatusPill } from '@/components/app/badges'
import { DataTable } from '@/components/app/DataTable'
import { Empty, NativeSelect, PageHeader, Section } from '@/components/app/common'
import { Button, buttonVariants } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { fileSize, money, ms, uploadTime, usd, when } from '@/lib/format'
import { cn } from '@/lib/utils'

// what the parser reads; .gz and .zip hold exports (a CUR is .csv.gz), drag and drop takes any file
const ACCEPT = '.pdf,.csv,.tsv,.xlsx,.xlsm,.xls,.json,.jsonl,.parquet,.gz,.zip,.yaml,.yml,.tf,.txt,.pptx,.docx,.md'

export function UploadPage() {
  const settings = useSettings()
  const tasks = useTasks()
  const docs = useDocuments()
  const start = useStartParse()
  const input = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [force, setForce] = useState(false)
  const [backend, setBackend] = useState<string>('')
  const [fallback, setFallback] = useState<string>('')
  const [dragging, setDragging] = useState(false)

  const add = (list: FileList | null) => {
    if (!list) return
    // copy now: the input is cleared right after this call, which empties its live FileList
    const picked = Array.from(list)
    setFiles((prev) => [...prev, ...picked.filter((f) => !prev.some((p) => p.name === f.name && p.size === f.size))])
  }

  const parse = async () => {
    for (const file of files) {
      try {
        await start.mutateAsync({ file, force, backend: backend || undefined, fallback: fallback || undefined })
      } catch (e) {
        toast.error(`${file.name}: ${e instanceof Error ? e.message : e}`)
      }
    }
    setFiles([])
  }

  const s = settings.data
  return (
    <>
      <PageHeader
        title="Upload"
        description="Bills, statements, cost exports, pricing estimates, inventories, Terraform and memos. PDFs need a text layer."
      />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div
          onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); add(e.dataTransfer.files) }}
          className={cn('flex min-h-44 flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed p-6 text-center transition-colors',
            dragging ? 'border-sky-500 bg-sky-50 dark:bg-sky-950/40' : 'border-border')}
        >
          <FileUpIcon className="size-8 text-muted-foreground" />
          <div className="text-sm">Drop files here, or</div>
          <Button variant="outline" onClick={() => input.current?.click()}>Choose files</Button>
          <input ref={input} type="file" multiple accept={ACCEPT} className="hidden" onChange={(e) => { add(e.target.files); e.target.value = '' }} />
          {files.length > 0 && (
            <ul className="w-full max-w-lg space-y-1 text-left text-sm">
              {files.map((f) => (
                <li key={f.name + f.size} className="flex items-center justify-between gap-2 rounded-md bg-muted/60 px-2 py-1">
                  <span className="truncate">{f.name}</span>
                  <span className="flex items-center gap-2 text-xs text-muted-foreground">
                    {fileSize(f.size)}
                    <button aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((x) => x !== f))}><XIcon className="size-3.5" /></button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="space-y-4 rounded-xl border p-4">
          <div className="flex items-start gap-2">
            <Checkbox id="force" checked={force} onCheckedChange={(v) => setForce(!!v)} />
            <Label htmlFor="force" className="block text-sm leading-snug font-normal">
              Parse again files already seen
              <span className="mt-0.5 block text-xs text-muted-foreground">
                A file seen before is always read again with the current specs, free. Tick this to also run again a learning job that ended without a spec, or improve the spec{s?.learner_enabled ? ' (costs money)' : ''}.
              </span>
            </Label>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="space-y-1 text-xs text-muted-foreground">
              Decision model
              <NativeSelect className="w-full" value={backend} onChange={(e) => setBackend(e.target.value)}>
                <option value="">default ({s?.decision_backend ?? '…'})</option>
                {s?.backends.map((b) => <option key={b} value={b}>{b}</option>)}
              </NativeSelect>
            </label>
            <label className="space-y-1 text-xs text-muted-foreground">
              Fallback
              <NativeSelect className="w-full" value={fallback} onChange={(e) => setFallback(e.target.value)}>
                <option value="">default ({s?.decision_fallback ?? '…'})</option>
                {s?.backends.map((b) => <option key={b} value={b}>{b}</option>)}
              </NativeSelect>
            </label>
          </div>
          {s && (
            <div className="text-xs text-muted-foreground">
              Keys: jev {s.keys.jev ? 'found' : 'not set'} · claude {s.keys.claude ? 'found' : 'not set'} · up to {s.max_upload_mb} MB per file
            </div>
          )}
          <Button className="w-full" disabled={!files.length || start.isPending} onClick={parse}>
            {start.isPending && <Loader2Icon className="animate-spin" />} Parse {files.length || ''} file{files.length === 1 ? '' : 's'}
          </Button>
        </div>
      </div>

      <Section title="Parses since the API started" className="mt-8">
        {tasks.data?.length ? <TaskTable tasks={tasks.data} /> : <Empty>No parse started yet.</Empty>}
      </Section>

      <Section title="Recent documents" className="mt-8" actions={<Link to="/documents" className="text-sm text-muted-foreground hover:underline">All documents</Link>}>
        <DataTable
          rows={(docs.data ?? []).slice(0, 12)}
          rowKey={(d) => d.sha256}
          height="auto"
          empty="Nothing parsed yet."
          columns={[
            { key: 'name', header: 'File', width: 'minmax(240px, 3fr)', render: (d) => <Link className="hover:underline" to={`/documents/${d.sha256}`}>{d.name}</Link> },
            { key: 'status', header: 'Status', width: '130px', render: (d) => <StatusPill status={d.status} /> },
            { key: 'completeness', header: 'Totals', width: '120px', render: (d) => <CompletenessPill value={d.completeness} /> },
            { key: 'lane', header: 'Read by', width: 'minmax(160px, 2fr)', render: (d) => <span className="text-muted-foreground">{d.lane}</span> },
            { key: 'by', header: 'Uploaded by', width: 'minmax(150px, 1.5fr)', render: (d) => <span className="text-muted-foreground">{d.uploaded_by ?? '—'}</span> },
            { key: 'ms', header: 'Time', width: '90px', align: 'right', render: (d) => <span title={`Last parse ${ms(d.elapsed_ms)}`}>{uploadTime(d.total_ms, d.elapsed_ms)}</span> },
            { key: 'when', header: 'Parsed', width: '150px', render: (d) => when(d.uploaded_at) },
          ]}
        />
      </Section>
    </>
  )
}

function TaskTable({ tasks }: { tasks: ParseTask[] }) {
  return (
    <div className="divide-y rounded-lg border">
      {tasks.map((t) => (
        <div key={t.job_id ?? t.sha256} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-sm">
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">{t.name}</div>
            {t.state === 'running' && (
              <div className="flex items-center gap-1.5 text-xs text-violet-700 dark:text-violet-300">
                <Loader2Icon className="size-3 animate-spin" /> {t.progress ?? 'Parsing…'}
              </div>
            )}
            {t.state === 'error' && <div className="text-xs break-words text-red-700 dark:text-red-300">{t.error}</div>}
            {t.state === 'done' && <Learning task={t} />}
          </div>
          {t.state === 'done' && (
            <>
              <StatusPill status={t.status} />
              <CompletenessPill value={t.completeness} />
              <ConfidencePill level={t.confidence} />
              <span className="text-xs text-muted-foreground tabular-nums">
                {t.charges} charges · {money(t.counted_total)} · {t.gaps} gaps · {t.cached ? 'stored result' : uploadTime(t.total_ms, t.elapsed_ms)} · {usd(t.cost_usd)}{t.started_by ? ` · ${t.started_by}` : ''}
              </span>
              <Link to={`/documents/${t.sha256}`} className={buttonVariants({ size: 'sm', variant: 'outline' })}>Open</Link>
            </>
          )}
          {t.state === 'running' && <Pill t="violet">running</Pill>}
          {t.state === 'error' && <Pill t="red">failed</Pill>}
        </div>
      ))}
    </div>
  )
}

// the learning run a parse queued: the first result is ready before it, and a proved spec replaces that result later
const LEARNED: Record<string, string> = {
  proved: 'learned a spec: the result was read again with it',
  unproved: 'ended without a spec that proves the file; the first result stands',
  limit: 'stopped at its limit; the first result stands',
  rejected: 'its spec broke files read before; the first result stands',
  stopped: 'stopped; the first result stands',
  error: 'failed; the first result stands',
}

function Learning({ task }: { task: ParseTask }) {
  const l = task.learning
  if (!l) return null
  if (learningActive(task)) {
    return (
      <div className="flex items-center gap-1.5 text-xs text-violet-700 dark:text-violet-300">
        <Loader2Icon className="size-3 animate-spin" />
        First result ready, learning a better spec{l.state === 'queued' ? ' (queued)' : ''}
        {l.progress ? ` · ${l.progress}` : ''}
      </div>
    )
  }
  const said = (l.agent_status && LEARNED[l.agent_status]) ?? (l.state === 'done' ? 'ended' : `ended ${l.state}`)
  return (
    <div className="text-xs text-muted-foreground">
      Learning {said}{l.cost_usd != null ? ` · ${usd(l.cost_usd)}` : ''}
    </div>
  )
}
