import { Loader2Icon, TriangleAlertIcon } from 'lucide-react'

import { cn } from '@/lib/utils'

export function PageHeader({ title, description, actions }: { title: React.ReactNode; description?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-balance break-words">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Loading({ what = 'Loading' }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
      <Loader2Icon className="size-4 animate-spin" /> {what}…
    </div>
  )
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = (error instanceof Error ? error.message : String(error ?? '')) || 'Something went wrong, with no message.'
  return (
    <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {onRetry && <button className="shrink-0 font-medium underline" onClick={onRetry}>Try again</button>}
    </div>
  )
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">{children}</div>
}

export function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="min-w-0 rounded-lg border bg-card px-3 py-2">
      <div className="text-xs text-muted-foreground" title={hint}>{label}</div>
      <div className="truncate text-sm font-medium tabular-nums">{value}</div>
    </div>
  )
}

/** A native select with the input styling (simple and keyboard friendly). */
export function NativeSelect({ className, ...props }: React.ComponentProps<'select'>) {
  return (
    <select
      className={cn(
        'h-8 rounded-lg border border-input bg-transparent px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30',
        className,
      )}
      {...props}
    />
  )
}

export function Section({ title, children, actions, className }: { title: React.ReactNode; children: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <section className={cn('space-y-2', className)}>
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  )
}

/** JSON shown folded; opens on click. */
export function JsonBlock({ value, summary = 'JSON', open = false }: { value: unknown; summary?: string; open?: boolean }) {
  return (
    <details open={open} className="rounded-lg border bg-muted/30 text-xs">
      <summary className="cursor-pointer px-3 py-1.5 text-muted-foreground select-none">{summary}</summary>
      <pre className="max-h-[480px] overflow-auto px-3 pb-3 font-mono whitespace-pre-wrap break-words">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  )
}
