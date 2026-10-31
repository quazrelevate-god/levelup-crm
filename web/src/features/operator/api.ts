/**
 * The operator console (docs/19): every client company, run from one place.
 * Platform operator only; the server refuses everyone else.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { Page } from '@/api/types'

export interface Company {
  readonly id: string
  readonly name: string
  readonly slug: string
  readonly is_active: boolean
  readonly owner_name: string | null
  readonly owner_email: string | null
  readonly seats_used: number
  readonly seat_limit: number
  readonly members: number
  readonly currency: string
  readonly timezone: string
  readonly default_country_code: string
  readonly balance: string | null
  readonly rate_per_minute: string | null
  readonly minutes_this_month: number
  readonly low_balance: boolean
  readonly support_active: boolean
  readonly features: Record<string, boolean>
  readonly created_at: string
}

export interface Overview {
  readonly companies: number
  readonly active_companies: number
  readonly licensed_users: number
  readonly wallet_totals: Record<string, string>
  readonly ai_calls_24h: number
  readonly low_balance_at: string
  readonly low_balance: readonly Company[]
  readonly pending_number_requests: number
  /** When the background worker last said it was alive; null if not running. */
  readonly worker_last_seen: string | null
}

export interface Feature {
  readonly key: string
  readonly label: string
  readonly description: string
  readonly on_by_default: boolean
}

export interface SignIn {
  /** A one-time set-your-password link; null when they already have an account. */
  readonly link: string | null
  readonly expires_at: string | null
}

export interface Role {
  readonly id: string
  readonly name: string
  readonly is_locked: boolean
  readonly is_admin: boolean
}

export interface Person {
  readonly membership_id: string
  readonly user_id: string
  readonly full_name: string
  readonly email: string
  readonly role_id: string
  readonly role_name: string
  readonly manager_id: string | null
  readonly is_active: boolean
  readonly has_license: boolean
  readonly is_support: boolean
  readonly created_at: string
}

export interface MoneyEntry {
  readonly id: string
  readonly kind: string
  readonly amount: string
  readonly balance_after: string
  readonly note: string | null
  readonly recorded_by: string | null
  readonly billed_minutes: number | null
  readonly created_at: string
}

export interface CompanyWallet {
  readonly currency: string
  readonly balance: string | null
  readonly rate_per_minute: string | null
  readonly low_balance_at: string
  readonly entries: readonly MoneyEntry[]
  readonly total_entries: number
}

export interface HistoryEntry {
  readonly id: string
  readonly actor_email: string
  readonly workspace_id: string | null
  readonly action: string
  readonly summary: string
  readonly created_at: string
}

export interface NumberRequest {
  readonly id: string
  readonly workspace_id: string
  readonly company: string
  readonly region: string | null
  readonly monthly_price: string
  readonly created_at: string
}

export interface NewCompany {
  readonly name: string
  readonly default_country_code: string
  readonly timezone: string
  readonly currency: string
  readonly seat_limit: number
  readonly features: Record<string, boolean>
  readonly opening_balance: string
  readonly rate_per_minute: string
  readonly owner_name: string
  readonly owner_email: string
}

const BASE = '/operator'
const keys = {
  all: ['operator'] as const,
  company: (id: string) => ['operator', 'company', id] as const,
}

function useInvalidate() {
  const client = useQueryClient()
  return () => client.invalidateQueries({ queryKey: keys.all })
}

export function useOverview() {
  return useQuery({
    queryKey: [...keys.all, 'overview'],
    queryFn: () => api.get<Overview>(`${BASE}/overview`),
    // Keeps the worker light honest without a reload.
    refetchInterval: 60_000,
  })
}

export function useFeatures() {
  return useQuery({
    queryKey: [...keys.all, 'features'],
    queryFn: () => api.get<Feature[]>(`${BASE}/features`),
    staleTime: Infinity,
  })
}

export function useCompanies() {
  return useQuery({
    queryKey: [...keys.all, 'companies'],
    queryFn: () => api.get<Company[]>(`${BASE}/companies`),
  })
}

export function useCompany(id: string) {
  return useQuery({
    queryKey: keys.company(id),
    queryFn: () => api.get<Company>(`${BASE}/companies/${id}`),
  })
}

export function useCreateCompany() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (body: NewCompany) =>
      api.post<{ company: Company; owner: SignIn }>(`${BASE}/companies`, body),
    onSuccess: () => void invalidate(),
  })
}

export function useUpdateCompany(id: string) {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (body: {
      name?: string
      seat_limit?: number
      features?: Record<string, boolean>
      is_active?: boolean
      timezone?: string
      default_country_code?: string
    }) => api.patch<Company>(`${BASE}/companies/${id}`, body),
    onSuccess: () => void invalidate(),
  })
}

export function useRoles(id: string) {
  return useQuery({
    queryKey: [...keys.company(id), 'roles'],
    queryFn: () => api.get<Role[]>(`${BASE}/companies/${id}/roles`),
  })
}

export function usePeople(id: string) {
  return useQuery({
    queryKey: [...keys.company(id), 'people'],
    queryFn: () => api.get<Person[]>(`${BASE}/companies/${id}/people`),
  })
}

export function useAddPerson(id: string) {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (body: {
      full_name: string
      email: string
      role_id: string
      manager_id: string | null
    }) => api.post<{ person: Person; sign_in: SignIn }>(`${BASE}/companies/${id}/people`, body),
    onSuccess: () => void invalidate(),
  })
}

export function useUpdatePerson(id: string) {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({
      membershipId,
      body,
    }: {
      membershipId: string
      body: {
        role_id?: string
        manager_id?: string
        clear_manager?: boolean
        is_active?: boolean
        reassign_to?: string
      }
    }) => api.patch<Person>(`${BASE}/companies/${id}/people/${membershipId}`, body),
    onSuccess: () => void invalidate(),
  })
}

export function useSignInLink(id: string) {
  return useMutation({
    mutationFn: (membershipId: string) =>
      api.post<SignIn>(`${BASE}/companies/${id}/people/${membershipId}/sign-in-link`, {}),
  })
}

export function useCompanyWallet(id: string, offset: number) {
  return useQuery({
    queryKey: [...keys.company(id), 'wallet', offset],
    queryFn: () =>
      api.get<CompanyWallet>(`${BASE}/companies/${id}/wallet`, { query: { limit: 20, offset } }),
  })
}

export function useMoney(id: string) {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (body: {
      kind: 'TOPUP' | 'DISCOUNT' | 'ADJUSTMENT'
      amount: string
      note: string
    }) => api.post<CompanyWallet>(`${BASE}/companies/${id}/wallet/entries`, body),
    onSuccess: () => void invalidate(),
  })
}

export function useSetRate(id: string) {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (rate: string) =>
      api.put<CompanyWallet>(`${BASE}/companies/${id}/wallet/rate`, { rate_per_minute: rate }),
    onSuccess: () => void invalidate(),
  })
}

export function useSupport(id: string) {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (enter: boolean) =>
      enter
        ? api.post<void>(`${BASE}/companies/${id}/support`, {})
        : api.delete<void>(`${BASE}/companies/${id}/support`),
    onSuccess: () => void invalidate(),
  })
}

export function useHistory(workspaceId: string | null, offset: number) {
  return useQuery({
    queryKey: [...keys.all, 'history', workspaceId, offset],
    queryFn: () =>
      api.get<Page<HistoryEntry>>(`${BASE}/history`, {
        query: { limit: 20, offset, ...(workspaceId ? { workspace_id: workspaceId } : {}) },
      }),
  })
}

export function useNumberRequests() {
  return useQuery({
    queryKey: [...keys.all, 'numbers'],
    queryFn: () => api.get<NumberRequest[]>(`${BASE}/number-requests`),
  })
}

export function useSettleNumber() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({
      workspaceId,
      requestId,
      phone,
    }: {
      workspaceId: string
      requestId: string
      phone: string | null
    }) =>
      api.post<void>(`${BASE}/companies/${workspaceId}/number-requests/${requestId}`, {
        phone_number: phone,
      }),
    onSuccess: () => void invalidate(),
  })
}

export function useOperatorAccounts() {
  return useQuery({
    queryKey: [...keys.all, 'operators'],
    queryFn: () => api.get<{ email: string; has_account: boolean }[]>(`${BASE}/operators`),
  })
}

export function useOperatorLink() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (email: string) => api.post<SignIn>(`${BASE}/operators/sign-in-link`, { email }),
    onSuccess: () => void invalidate(),
  })
}
