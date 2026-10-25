import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  api,
  ApiError,
  qs,
  type ApproveRequest,
  type ChargeDetail,
  type CostsOut,
  type DocumentItem,
  type DocumentView,
  type MappingPage,
  type ParseTask,
  type Resolution,
  type ReviewEdits,
  type ReviewPreview,
  type Settings,
  type SourceInfo,
  type SourceLine,
  type SourceTable,
} from './client'

export type Job = {
  id: number
  layout_key: string
  sha256: string | null
  name: string
  status: string
  spec_id: string | null
  spec_status: string | null
  attempts: number
  created: number
  updated: number
  error: string | null
  cost_usd: number | null
  started: number | null
  seconds: number | null
  report?: Record<string, unknown> | null
}
export type SpecSummary = {
  id: string
  name: string
  version: number
  status: 'draft' | 'trusted' | 'disabled'
  engine: string
  priority: number
  verified: number
  updated_at: number
}
export type SpecVersion = { version: number; saved_at: number; spec: Record<string, unknown> }
export type TraceEvent = Record<string, unknown> & { event: string; ts?: number; round?: number }
export type Alias = { provider: string; label: string; label_norm: string; code: string; source: string; created: number }

export const keys = {
  settings: ['settings'] as const,
  documents: ['documents'] as const,
  view: (sha: string) => ['document', sha] as const,
  charge: (sha: string, id: string) => ['document', sha, 'charge', id] as const,
  source: (sha: string) => ['document', sha, 'source'] as const,
  tasks: ['tasks'] as const,
  task: (sha: string) => ['tasks', sha] as const,
  reviewQueue: ['review-queue'] as const,
  jobs: ['jobs'] as const,
  job: (id: number) => ['jobs', id] as const,
  costs: ['costs'] as const,
  specs: ['specs'] as const,
  spec: (id: string) => ['specs', id] as const,
  aliases: ['aliases'] as const,
}

export const useSettings = () =>
  useQuery({ queryKey: keys.settings, queryFn: () => api.get<Settings>('/settings'), staleTime: Infinity })

export const useDocuments = () =>
  useQuery({ queryKey: keys.documents, queryFn: () => api.get<DocumentItem[]>('/documents') })

/** A document's view holds every charge: it is kept briefly after its page closes (it can be large). */
export const useDocumentView = (sha: string) =>
  useQuery({
    queryKey: keys.view(sha),
    queryFn: () => api.get<DocumentView>(`/documents/${sha}/view`),
    gcTime: 60_000,
    staleTime: 30_000,
    // 404 / 409 (no such document, or no result yet) will not change by asking again at once
    retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 1,
  })

export const useChargeDetail = (sha: string, id: string | null) =>
  useQuery({
    queryKey: keys.charge(sha, id ?? ''),
    queryFn: () => api.get<ChargeDetail>(`/documents/${sha}/charges/${encodeURIComponent(id ?? '')}`),
    enabled: !!id,
    gcTime: 30_000,
  })

export const useSourceInfo = (sha: string) =>
  useQuery({ queryKey: keys.source(sha), queryFn: () => api.get<SourceInfo>(`/documents/${sha}/source`) })

export const useSourceTable = (sha: string, name: string | null, offset: number, limit: number) =>
  useQuery({
    queryKey: [...keys.source(sha), 'table', name, offset, limit],
    queryFn: () =>
      api.get<SourceTable>(`/documents/${sha}/source/table${qs({ name: name ?? '', offset, limit })}`),
    enabled: !!name,
    placeholderData: (prev) => prev,
  })

export const useSourceLines = (sha: string, page: number | null) =>
  useQuery({
    queryKey: [...keys.source(sha), 'lines', page],
    queryFn: () => api.get<SourceLine[]>(`/documents/${sha}/source/lines${qs({ page })}`),
  })

const anyRunning = (tasks?: ParseTask[]) => !!tasks?.some((t) => t.state === 'running')

export const useTasks = () =>
  useQuery({
    queryKey: keys.tasks,
    queryFn: () => api.get<ParseTask[]>('/parse/tasks'),
    refetchInterval: (q) => (anyRunning(q.state.data) ? 1500 : false),
  })

export const useTask = (sha: string, enabled = true) =>
  useQuery({
    queryKey: keys.task(sha),
    queryFn: () => api.get<ParseTask>(`/parse/tasks/${sha}`),
    enabled,
    retry: false,
    refetchInterval: (q) => (q.state.data?.state === 'running' ? 1500 : false),
  })

export function useStartParse() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ file, force, backend, fallback }: { file: File; force: boolean; backend?: string; fallback?: string }) => {
      const form = new FormData()
      form.append('file', file)
      return api.upload<ParseTask>(`/parse/start${qs({ force, backend, fallback })}`, form)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.tasks }),
  })
}

export function useReparse() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sha: string) => api.post<ParseTask>(`/documents/${sha}/reparse`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.tasks }),
  })
}

export function useParseCurrent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sha: string) => api.post<ParseTask>(`/documents/${sha}/parse-current`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.tasks }),
  })
}

export function useDeleteDocument() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sha: string) => api.del<{ deleted: string }>(`/documents/${sha}`),
    onSuccess: (_, sha) => {
      qc.removeQueries({ queryKey: keys.view(sha) })
      qc.invalidateQueries({ queryKey: keys.documents })
      qc.invalidateQueries({ queryKey: keys.reviewQueue })
    },
  })
}

export const useReviewQueue = () =>
  useQuery({ queryKey: keys.reviewQueue, queryFn: () => api.get<DocumentItem[]>('/review/queue') })

export const usePreview = (sha: string) =>
  useMutation({ mutationFn: (edits: ReviewEdits) => api.post<ReviewPreview>(`/documents/${sha}/review/preview`, edits) })

export function useApprove(sha: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (req: ApproveRequest) => api.post<{ status: string }>(`/documents/${sha}/review/approve`, req),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.view(sha) })
      qc.invalidateQueries({ queryKey: keys.documents })
      qc.invalidateQueries({ queryKey: keys.reviewQueue })
    },
  })
}

export const useJobs = () =>
  useQuery({
    queryKey: keys.jobs,
    queryFn: () => api.get<Job[]>('/learn/jobs'),
    refetchInterval: (q) => (q.state.data?.some((j) => j.status === 'running') ? 3000 : false),
  })

export const useJob = (id: number) => useQuery({ queryKey: keys.job(id), queryFn: () => api.get<Job>(`/learn/jobs/${id}`) })

export const useJobTrace = (id: number, running: boolean) =>
  useQuery({
    queryKey: [...keys.job(id), 'trace'],
    queryFn: () => api.get<TraceEvent[]>(`/learn/jobs/${id}/trace`),
    refetchInterval: running ? 3000 : false,
  })

export function useRetryJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<{ id: number; status: string }>(`/learn/jobs/${id}/retry`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.jobs }),
  })
}

export function useStopJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<{ id: number; status: string }>(`/learn/jobs/${id}/stop`),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: keys.jobs })
      qc.invalidateQueries({ queryKey: keys.job(id) })
    },
  })
}

export function useStopDocument() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sha: string) => api.post<{ stopped: number[] }>(`/documents/${sha}/stop`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.jobs })
      qc.invalidateQueries({ queryKey: keys.documents })
    },
  })
}

export const useCosts = () => useQuery({ queryKey: keys.costs, queryFn: () => api.get<CostsOut>('/costs') })

export const useSpecs = () => useQuery({ queryKey: keys.specs, queryFn: () => api.get<SpecSummary[]>('/specs') })

export const useSpec = (id: string) =>
  useQuery({ queryKey: keys.spec(id), queryFn: () => api.get<Record<string, unknown>>(`/specs/${encodeURIComponent(id)}`) })

export const useSpecHistory = (id: string) =>
  useQuery({
    queryKey: [...keys.spec(id), 'history'],
    queryFn: () => api.get<SpecVersion[]>(`/specs/${encodeURIComponent(id)}/history`),
  })

export function useSetSpecStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: SpecSummary['status'] }) =>
      api.patch<{ id: string; status: string }>(`/specs/${encodeURIComponent(id)}/status`, { status }),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.specs }),
  })
}

export const useMapping = (q: string, provider: string, view: 'all' | 'flagged' | 'ambiguous', offset: number) =>
  useQuery({
    queryKey: ['mapping', q, provider, view, offset],
    queryFn: () => api.get<MappingPage>(`/mapping${qs({ q, provider, view, offset, limit: 200 })}`),
    placeholderData: (prev) => prev,
  })

export const useResolve = (label: string, provider: string) =>
  useQuery({
    queryKey: ['resolve', label, provider],
    queryFn: () => api.get<Resolution>(`/mapping/resolve${qs({ label, provider })}`),
    enabled: label.trim().length > 0,
  })

export const useAliases = () => useQuery({ queryKey: keys.aliases, queryFn: () => api.get<Alias[]>('/aliases') })

export function useAddAlias() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (a: { provider: 'aws' | 'azure' | 'gcp'; label: string; code: string }) => api.post('/aliases', a),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.aliases }),
  })
}

export function useDeleteAlias() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (a: { provider: string; label_norm: string }) =>
      api.del(`/aliases/${a.provider}/${encodeURIComponent(a.label_norm)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.aliases }),
  })
}
