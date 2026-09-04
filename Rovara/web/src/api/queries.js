/**
 * TanStack Query hooks. One per endpoint, so a component never constructs a URL.
 *
 * **Nothing here transforms a figure.** A `select` that reshaped a response would be the beginning
 * of client-side computation, and the whole arrangement of this app depends on the browser being a
 * renderer. Hooks return the server's document as it arrived.
 *
 * **Retries are off for mutations and now off for reads too.** Ownership assignment and consent
 * recording are not idempotent from the operator's point of view — a retried consent write is a
 * second `customer_consent` row with a later `effective_at`, which supersedes the first. Reads used
 * to retry once, on the reasoning that a dashboard hiding a transient blip is friendlier than one
 * that does not. That reasoning was wrong about what the reader experiences: a single retry does not
 * hide a slow request, it *doubles* the wait before the failure is admitted, because the retry only
 * starts once the first attempt has already spent its full latency. An operator watching a spinner
 * for twice as long, and then being told it failed anyway, was paying for a kindness they never
 * received. A failed read now surfaces at once and the reader can retry it themselves, knowing they
 * are doing so.
 *
 * `useWebhookHealth` keeps its own `retry: 1` and its 60s poll, deliberately. It does not spread
 * `READ_OPTIONS` and is not governed by the change above — see the note on the hook.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { DASHBOARD_KEY_HEADER, request, storeToken } from './client'

/**
 * Shared options for every read. Mutations set their own `retry: false` and do not spread this.
 *
 * `retry: 0` because a retry on a read buys nothing a reader wants. See the module note above.
 *
 * `staleTime: 45_000` replaces 15s. At 15s a reader who glances at a panel, scrolls, and glances
 * back has already crossed the threshold, so ordinary reading of the dashboard triggered refetches —
 * and every refetch re-pays the fixed server-side auth preamble before it can even begin to answer
 * the question asked. 45s is chosen rather than 60s so that the interval stays clear of
 * `useWebhookHealth`'s 60s poll: the two firing on the same tick would bunch requests, and webhook
 * health is the one read whose timing should not be competing with anything. It is also short enough
 * that no figure on screen is ever a minute old, which is the bound that matters — these are recovery
 * cases moving on a multi-hour window, not a ticking price.
 */
const READ_OPTIONS = {
  retry: 0,
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
