import { cn } from '@/lib/utils'
import { label } from '@/lib/format'

const tone = {
  green: 'bg-emerald-50 text-emerald-800 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300',
  amber: 'bg-amber-50 text-amber-800 ring-amber-600/25 dark:bg-amber-950 dark:text-amber-300',
  red: 'bg-red-50 text-red-800 ring-red-600/20 dark:bg-red-950 dark:text-red-300',
  blue: 'bg-sky-50 text-sky-800 ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300',
  violet: 'bg-violet-50 text-violet-800 ring-violet-600/20 dark:bg-violet-950 dark:text-violet-300',
  gray: 'bg-muted text-muted-foreground ring-border',
} as const
export type Tone = keyof typeof tone

export function Pill({ children, t = 'gray', className }: { children: React.ReactNode; t?: Tone; className?: string }) {
  return (
    <span className={cn('inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset', tone[t], className)}>
      {children}
    </span>
  )
}

const STATUS: Record<string, Tone> = {
  auto_accepted: 'green', needs_review: 'amber', approved: 'blue', processing: 'violet', interrupted: 'red',
}
const COMPLETENESS: Record<string, Tone> = { reconciled: 'green', consistent: 'green', unchecked: 'gray', contradicted: 'red' }
const CONFIDENCE: Record<string, Tone> = { high: 'green', medium: 'amber', low: 'red' }
const JOB: Record<string, Tone> = { saved: 'green', running: 'violet', queued: 'gray', skipped: 'gray', failed: 'red', rejected: 'amber' }
const SPEC: Record<string, Tone> = { trusted: 'green', draft: 'amber', disabled: 'gray' }

export const StatusPill = ({ status }: { status?: string | null }) => <Pill t={STATUS[status ?? ''] ?? 'gray'}>{label(status)}</Pill>
export const CompletenessPill = ({ value }: { value?: string | null }) => <Pill t={COMPLETENESS[value ?? ''] ?? 'gray'}>{label(value)}</Pill>
export const ConfidencePill = ({ level }: { level?: string | null }) =>
  level ? <Pill t={CONFIDENCE[level] ?? 'gray'}>confidence {level}</Pill> : <Pill>confidence —</Pill>
export const JobPill = ({ status }: { status?: string | null }) => <Pill t={JOB[status ?? ''] ?? 'gray'}>{label(status)}</Pill>
export const SpecPill = ({ status }: { status?: string | null }) => <Pill t={SPEC[status ?? ''] ?? 'gray'}>{label(status)}</Pill>
