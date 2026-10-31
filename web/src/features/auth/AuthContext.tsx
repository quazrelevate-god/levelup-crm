/**
 * Session state: who is signed in, which workspace they are working in, and
 * what that workspace's permission template grants them.
 *
 * The active workspace is deliberately *not* baked into the access token — the
 * API re-checks membership per request, so switching workspaces is a client
 * concern only, and a revoked membership stops working immediately rather than
 * at the next refresh.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { api, configureAuth, setTokens, type TokenPair } from '@/api/client'
import type { MeResponse, MembershipSummary, TokenResponse, UserSummary } from '@/api/types'
import { AuthContext, type AuthState } from '@/features/auth/context'

const TOKENS_KEY = 'crm.tokens'
const ACTIVE_WORKSPACE_KEY = 'crm.activeWorkspaceId'

function readStoredTokens(): TokenPair | null {
  const raw = localStorage.getItem(TOKENS_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<TokenPair>
    if (typeof parsed.accessToken === 'string' && typeof parsed.refreshToken === 'string') {
      return { accessToken: parsed.accessToken, refreshToken: parsed.refreshToken }
    }
  } catch {
    // Corrupt entry: treat it as no session rather than crashing on boot.
  }
  return null
}

function persistTokens(next: TokenPair | null): void {
  if (next) {
    localStorage.setItem(TOKENS_KEY, JSON.stringify(next))
  } else {
    localStorage.removeItem(TOKENS_KEY)
  }
}

export function AuthProvider({ children }: { readonly children: ReactNode }) {
  const [status, setStatus] = useState<AuthState['status']>('loading')
  const [user, setUser] = useState<UserSummary | null>(null)
  const [memberships, setMemberships] = useState<readonly MembershipSummary[]>([])
  const [isOperator, setIsOperator] = useState(false)
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(() =>
    localStorage.getItem(ACTIVE_WORKSPACE_KEY),
  )

  const clearSession = useCallback(() => {
    setTokens(null)
    persistTokens(null)
    localStorage.removeItem(ACTIVE_WORKSPACE_KEY)
    setUser(null)
    setMemberships([])
    setIsOperator(false)
    setActiveWorkspaceId(null)
    setStatus('anonymous')
  }, [])

  // Wire the transport's refresh hooks once. `onSessionEnded` fires when a
  // refresh fails — an expired or revoked family — and is the only path that
  // logs someone out without them asking.
  useEffect(() => {
    configureAuth({
      onSessionEnded: clearSession,
      onTokensRefreshed: persistTokens,
    })
  }, [clearSession])

  // Restore a session from storage on boot.
  useEffect(() => {
    const stored = readStoredTokens()
    if (!stored) {
      setStatus('anonymous')
      return
    }

    setTokens(stored)
    let cancelled = false

    api
      .get<MeResponse>('/me')
      .then((me) => {
        if (cancelled) return
        setUser(me.user)
        setMemberships(me.memberships)
        setIsOperator(me.is_platform_operator)
        setStatus('authenticated')
      })
      .catch(() => {
        if (!cancelled) clearSession()
      })

    return () => {
      cancelled = true
    }
  }, [clearSession])

  const login = useCallback(async (email: string, password: string) => {
    const response = await api.post<TokenResponse>(
      '/auth/login',
      { email, password },
      { anonymous: true },
    )

    const pair: TokenPair = {
      accessToken: response.access_token,
      refreshToken: response.refresh_token,
    }
    setTokens(pair)
    persistTokens(pair)
    setUser(response.user)
    setMemberships(response.memberships)
    const operator = Boolean(response.is_platform_operator)
    setIsOperator(operator)
    setStatus('authenticated')

    // Land in a workspace the user can actually work in. A membership without
    // a licence is refused by every scoped endpoint, so selecting one would
    // mean an app full of 403s.
    const usable = response.memberships.filter((m) => m.is_active && m.has_license)
    const remembered = localStorage.getItem(ACTIVE_WORKSPACE_KEY)
    const restored = usable.find((m) => m.workspace.id === remembered)
    const chosen = restored ?? (usable.length === 1 ? usable[0] : undefined)

    if (chosen) {
      localStorage.setItem(ACTIVE_WORKSPACE_KEY, chosen.workspace.id)
      setActiveWorkspaceId(chosen.workspace.id)
    } else {
      localStorage.removeItem(ACTIVE_WORKSPACE_KEY)
      setActiveWorkspaceId(null)
    }
    return { isOperator: operator }
  }, [])

  const reload = useCallback(async () => {
    const me = await api.get<MeResponse>('/me')
    setUser(me.user)
    setMemberships(me.memberships)
    setIsOperator(me.is_platform_operator)
  }, [])

  const logout = useCallback(async () => {
    const stored = readStoredTokens()
    if (stored) {
      // Best effort: the session ends locally regardless of whether the server
      // acknowledged, so a network failure here must not strand the user.
      await api
        .post('/auth/logout', { refresh_token: stored.refreshToken }, { anonymous: true })
        .catch(() => undefined)
    }
    clearSession()
  }, [clearSession])

  const selectWorkspace = useCallback((workspaceId: string) => {
    localStorage.setItem(ACTIVE_WORKSPACE_KEY, workspaceId)
    setActiveWorkspaceId(workspaceId)
  }, [])

  const activeMembership = useMemo(
    () => memberships.find((m) => m.workspace.id === activeWorkspaceId) ?? null,
    [memberships, activeWorkspaceId],
  )

  const value = useMemo<AuthState>(
    () => ({
      status,
      user,
      memberships,
      activeWorkspaceId,
      activeMembership,
      isOperator,
      login,
      logout,
      reload,
      selectWorkspace,
    }),
    [
      status,
      user,
      memberships,
      activeWorkspaceId,
      activeMembership,
      isOperator,
      login,
      logout,
      reload,
      selectWorkspace,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
