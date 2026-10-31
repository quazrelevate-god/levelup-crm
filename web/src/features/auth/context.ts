/**
 * The auth context object and its hook.
 *
 * Split out from `AuthContext.tsx` so that file exports only a component —
 * mixing components and plain values in one module breaks React Fast Refresh,
 * which would reload the whole app on every edit to a screen.
 */

import { createContext, useContext } from 'react'

import type { MembershipSummary, UserSummary } from '@/api/types'

export interface AuthState {
  readonly status: 'loading' | 'authenticated' | 'anonymous'
  readonly user: UserSummary | null
  readonly memberships: readonly MembershipSummary[]
  readonly activeWorkspaceId: string | null
  readonly activeMembership: MembershipSummary | null
  /** The platform operator (docs/19): sees the operator console. */
  readonly isOperator: boolean
  /** Resolves to whether the person signing in is the operator. */
  login: (email: string, password: string) => Promise<{ isOperator: boolean }>
  /** Re-read memberships, e.g. after the operator goes inside a company. */
  reload: () => Promise<void>
  logout: () => Promise<void>
  selectWorkspace: (workspaceId: string) => void
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside an AuthProvider')
  }
  return context
}
