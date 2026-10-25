import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, AUTH_REQUIRED, type Me } from './client'

const ME = ['auth', 'me'] as const

export function useMe() {
  const qc = useQueryClient()
  useEffect(() => {
    const again = () => qc.invalidateQueries({ queryKey: ME })
    window.addEventListener(AUTH_REQUIRED, again)
    return () => window.removeEventListener(AUTH_REQUIRED, again)
  }, [qc])
  return useQuery({ queryKey: ME, queryFn: () => api.get<Me>('/auth/me'), staleTime: 60_000, retry: false })
}

export function useInvite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (code: string) => api.post<Me>('/auth/invite', { code }),
    onSuccess: (me) => qc.setQueryData(ME, me),
  })
}

export function useLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<Me>('/auth/logout'),
    onSuccess: (me) => {
      qc.clear()
      qc.setQueryData(ME, me)
    },
  })
}

/** Google sign-in starts with a full page load (the API redirects to Google and back). */
export function signInUrl() {
  return `/api/auth/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`
}
