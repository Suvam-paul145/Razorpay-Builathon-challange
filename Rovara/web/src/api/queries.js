/**
 * TanStack Query hooks. One per endpoint, so a component never constructs a URL.
 *
 * **Nothing here transforms a figure.** A `select` that reshaped a response would be the beginning
 * of client-side computation, and the whole arrangement of this app depends on the browser being a
 * renderer. Hooks return the server's document as it arrived.
 *
 * **Retries are off for mutations and limited for reads.** Ownership assignment and consent
 * recording are not idempotent from the operator's point of view — a retried consent write is a
 * second `customer_consent` row with a later `effective_at`, which supersedes the first. Reads retry
 * once, because a dashboard that hides a transient blip is friendlier than one that does not, and a
 * stale read is harmless where a duplicated write is not.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { DASHBOARD_KEY_HEADER, request, storeToken } from './client'

const READ_OPTIONS = {
  retry: 1,
  staleTime: 15_000,
  refetchOnWindowFocus: false,
}

/**
 * @param {string | null} state
 * @param {number} offset
 */
export function useCases(state, offset) {
  const params = new URLSearchParams()
  if (state !== null) params.set('state', state)
  if (offset > 0) params.set('offset', String(offset))
  const suffix = params.toString()
  return useQuery({
    queryKey: ['cases', state, offset],
    queryFn: () => request(`/cases${suffix === '' ? '' : `?${suffix}`}`),
    ...READ_OPTIONS,
  })
}

/** @param {string} caseId */
export function useCaseDetail(caseId) {
  return useQuery({
    queryKey: ['case', caseId],
    queryFn: () => request(`/cases/${caseId}`),
    ...READ_OPTIONS,
  })
}

/** @param {string} caseId */
export function useAuditTrail(caseId) {
  return useQuery({
    queryKey: ['audit', caseId],
    queryFn: () => request(`/cases/${caseId}/audit`),
    ...READ_OPTIONS,
  })
}

export function useMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: () => request('/metrics/summary'),
    ...READ_OPTIONS,
  })
}

export function useUnresolved() {
  return useQuery({
    queryKey: ['unresolved'],
    queryFn: () => request('/metrics/unresolved'),
    ...READ_OPTIONS,
  })
}

export function useExperiments() {
  return useQuery({
    queryKey: ['experiments'],
    queryFn: () => request('/experiments'),
    ...READ_OPTIONS,
  })
}

export function useWebhookHealth() {
  return useQuery({
    queryKey: ['webhook-health'],
    queryFn: () => request('/health/webhook'),
    // Polled, unlike everything else here. A disabled webhook means *silent total detection loss* —
    // no cases open at all — so this is the one figure whose staleness is itself the risk.
    refetchInterval: 60_000,
    retry: 1,
    refetchOnWindowFocus: true,
  })
}

export function useSignIn() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ merchantSlug, dashboardKey }) => {
      const session = await request('/auth/sessions', {
        method: 'POST',
        anonymous: true,
        body: { merchant_slug: merchantSlug },
        headers: { [DASHBOARD_KEY_HEADER]: dashboardKey },
      })
      storeToken(session.token)
      return session
    },
    // Everything cached was fetched as nobody, or as a previous tenant. Clearing rather than
    // invalidating: a stale case list belonging to another merchant must not be renderable for even
    // one frame while a refetch is in flight.
    onSuccess: () => {
      queryClient.clear()
    },
    retry: false,
  })
}

/** @param {string} caseId */
export function useAssignOwnership(caseId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => request(`/cases/${caseId}/owner`, { method: 'POST' }),
    onSuccess: () => {
      // The case *and* its audit trail: assignment writes a HUMAN_OWNER_ASSIGNED record, and a trail
      // that did not show it would make the suppression of automated action look unexplained.
      void queryClient.invalidateQueries({ queryKey: ['case', caseId] })
      void queryClient.invalidateQueries({ queryKey: ['audit', caseId] })
    },
    retry: false,
  })
}

/** @param {string} caseId */
export function useReleaseOwnership(caseId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => request(`/cases/${caseId}/owner`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['case', caseId] })
      void queryClient.invalidateQueries({ queryKey: ['audit', caseId] })
    },
    retry: false,
  })
}

export function useRecordConsent() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ contact, optedOut, source }) =>
      request('/consent', {
        method: 'POST',
        body: { contact, opted_out: optedOut, source },
      }),
    onSuccess: () => {
      // An opt-out is keyed on the customer, not the payment, so it governs cases that already exist
      // as well as ones that do not yet. Every case view is potentially stale.
      void queryClient.invalidateQueries({ queryKey: ['cases'] })
    },
    retry: false,
  })
}
