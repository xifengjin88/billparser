import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { Loader2Icon } from 'lucide-react'

import { signInUrl, useInvite, useLogout, useMe } from '@/api/auth'
import { ErrorBox, Loading } from '@/components/app/common'
import { Button, buttonVariants } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

const ERRORS: Record<string, string> = {
  domain: 'Only verified accounts of the company domain can sign in. Choose your work account.',
  google: 'Google sign-in did not finish. Try again.',
}

/** The app shows only after a sign-in (AUTH=google): Google first, then an invite code once. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const me = useMe()
  if (me.isLoading) return <Loading what="Checking your sign-in" />
  if (me.error) return <Centered><ErrorBox error={me.error} /></Centered>
  const m = me.data!
  if (m.state === 'ok') return <>{children}</>
  if (m.state === 'invite_required') return <InviteScreen email={m.email ?? ''} />
  return <SignInScreen domain={m.domain ?? ''} />
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh items-center justify-center bg-muted/30 px-4">
      <div className="w-full max-w-sm space-y-4 rounded-xl border bg-background p-6 shadow-sm">{children}</div>
    </div>
  )
}

function SignInScreen({ domain }: { domain: string }) {
  const [params] = useSearchParams()
  const error = params.get('auth_error')
  return (
    <Centered>
      <div>
        <div className="text-lg font-semibold">Invoice Parser</div>
        <p className="mt-1 text-sm text-muted-foreground">Sign in with your {domain ? `@${domain}` : 'work'} Google account.</p>
      </div>
      {error && <ErrorBox error={ERRORS[error] ?? 'Sign-in failed.'} />}
      <a href={signInUrl()} className={buttonVariants({ className: 'w-full' })}>Sign in with Google</a>
    </Centered>
  )
}

function InviteScreen({ email }: { email: string }) {
  const [code, setCode] = useState('')
  const invite = useInvite()
  const logout = useLogout()
  return (
    <Centered>
      <div>
        <div className="text-lg font-semibold">Enter your invite code</div>
        <p className="mt-1 text-sm text-muted-foreground">Signed in as {email}. You need a code only the first time.</p>
      </div>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); invite.mutate(code) }}>
        <Input id="invite" autoFocus autoComplete="off" placeholder="Invite code" value={code} onChange={(e) => setCode(e.target.value)} />
        {invite.error && <ErrorBox error={invite.error} />}
        <Button type="submit" className="w-full" disabled={!code.trim() || invite.isPending}>
          {invite.isPending && <Loader2Icon className="animate-spin" />} Continue
        </Button>
      </form>
      <Button variant="ghost" className="w-full" onClick={() => logout.mutate()}>Use another account</Button>
    </Centered>
  )
}

/** Who is signed in, and a way out (only when sign-in is on). */
export function UserMenu() {
  const me = useMe()
  const logout = useLogout()
  if (me.data?.auth !== 'google' || me.data.state !== 'ok') return null
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="truncate" title={me.data.email ?? ''}>{me.data.email}</span>
      <button className="shrink-0 hover:text-foreground hover:underline" onClick={() => logout.mutate()}>Sign out</button>
    </div>
  )
}
