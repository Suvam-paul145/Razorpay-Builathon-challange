/**
 * TanStack Query hooks. One per endpoint, so a component never constructs a URL.
 *
 * **Nothing here transforms a figure.** A `select` that reshaped a response would be the beginning
 * of client-side computation, and the whole arrangement of this app depends on the browser being a
 * renderer. Hooks return the server's document as it arrived.
 *
 * **Retries are off for mutations, and now for reads as well.** Ownership assignment and consent
 * recording are not idempotent from the operator's point of view — a retried consent write is a
 * second `customer_consent` row with a later `effective_at`, which supersedes the first. Reads no
 * longer retry either: against a slow backend one retry doubles the wall-clock wait before anything
 * appears, so an operator sits through two full round trips to be told once that a read failed, and
 * each attempt re-pays the server's authenticated preamble. Hiding a transient blip was not worth
 * that. The one exception is the webhook health poll, which keeps its retry because it is sampled on
 * a fixed cadence and a dropped sample there is a gap in a liveness reading, not a page that failed.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { DASHBOARD_KEY_HEADER, request, storeToken } from './client'

// Reads only. Every mutation below sets `retry: false` on itself and none of them spread this, which
// is what keeps a change here from quietly making a non-idempotent write retryable.
const READ_OPTIONS = {
  retry: 0,
  // 45s, against a dashboard whose figures are projections of a pipeline that moves in minutes. At
  // 15s the app refetched more or less continuously and re-paid the authenticated preamble each
  // time, buying freshness no reader could perceive.
  staleTime: 45_000,
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

/**
 * The nine-stage timeline for one case (R26.C1).
 *
 * Its own query rather than a field of `useCaseDetail`, and the separation is deliberate. The
 * timeline read is bounded by `TIMELINE_QUERY_TIMEOUT` on the server and degrades on its own: when it
 * cannot be projected in time the response says so and names the case, while every other section of
 * the detail view still renders from its own request. Folding it into the detail query would make a
 * slow audit trail blank the whole page — which is the failure R26.C10 is written to prevent.
 *
 * No `select` and no transform, like every other hook here. The document arrives projected.
 *
 * @param {string} caseId
 */
export function useCaseTimeline(caseId) {
  return useQuery({
    queryKey: ['timeline', caseId],
    queryFn: () => request(`/cases/${caseId}/timeline`),
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
