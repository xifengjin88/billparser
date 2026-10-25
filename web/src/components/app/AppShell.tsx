import { NavLink, Outlet } from 'react-router'
import {
  BotIcon, CoinsIcon, FileStackIcon, FileUpIcon, ListChecksIcon, Loader2Icon, NetworkIcon, ScrollTextIcon,
} from 'lucide-react'

import { useSettings, useTasks } from '@/api/queries'
import { AuthGate, UserMenu } from '@/components/app/AuthGate'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/upload', label: 'Upload', icon: FileUpIcon },
  { to: '/documents', label: 'Documents', icon: FileStackIcon },
  { to: '/review', label: 'Review', icon: ListChecksIcon },
  { to: '/jobs', label: 'Learning jobs', icon: BotIcon },
  { to: '/costs', label: 'Costs', icon: CoinsIcon },
  { to: '/specs', label: 'Layout specs', icon: ScrollTextIcon },
  { to: '/mapping', label: 'Service mapping', icon: NetworkIcon },
]

export function AppShell() {
  return (
    <AuthGate>
      <Shell />
    </AuthGate>
  )
}

function Shell() {
  const settings = useSettings()
  const tasks = useTasks()
  const running = tasks.data?.filter((t) => t.state === 'running').length ?? 0
  return (
    <div className="flex min-h-svh bg-background text-foreground">
      <aside className="sticky top-0 hidden h-svh w-56 shrink-0 flex-col border-r bg-muted/30 md:flex">
        <div className="px-4 pt-4 pb-3">
          <div className="text-sm font-semibold tracking-tight">Invoice Parser</div>
          <div className="text-xs text-muted-foreground">bills · exports · estimates</div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn('flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground',
                  isActive && 'bg-background font-medium text-foreground shadow-xs ring-1 ring-border')
              }
            >
              <Icon className="size-4" /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto space-y-1 border-t px-4 py-3 text-xs text-muted-foreground">
          <UserMenu />
          {running > 0 && (
            <NavLink to="/upload" className="flex items-center gap-1.5 text-violet-700 dark:text-violet-300">
              <Loader2Icon className="size-3.5 animate-spin" /> {running} parse{running > 1 ? 's' : ''} running
            </NavLink>
          )}
          {settings.data && (
            <>
              <div>Decisions: {settings.data.decision_backend} → {settings.data.decision_fallback}</div>
              <div>Learning: {settings.data.learner_enabled ? 'on (costs money)' : 'off'}</div>
            </>
          )}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <nav className="flex gap-1 overflow-x-auto border-b px-4 py-2 md:hidden">
          {NAV.map(({ to, label }) => (
            <NavLink key={to} to={to} className={({ isActive }) => cn('rounded-md px-2 py-1 text-sm whitespace-nowrap', isActive ? 'bg-muted font-medium' : 'text-muted-foreground')}>
              {label}
            </NavLink>
          ))}
        </nav>
        <main className="mx-auto w-full max-w-[1500px] min-w-0 flex-1 px-4 py-6 md:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
