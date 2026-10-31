/**
 * HTTP transport.
 *
 * Two things live here that the rest of the app must not reimplement:
 *
 * 1. **The error shape.** The API always answers `{detail: {code, message}}`,
 *    so `ApiError` carries the code. Screens branch on `error.code`, never on
 *    a parsed message string.
 * 2. **Token refresh.** A 401 triggers one refresh and one retry. Concurrent
 *    401s share a single in-flight refresh — without that, five parallel
 *    queries would each rotate the token, and four of the five rotations would
 *    be replays that trip the server's reuse detection and log the user out.
 */

const DEFAULT_API_BASE_URL = 'http://localhost:8000'

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL
export const API_V1_URL = `${API_BASE_URL}/api/v1`

export class ApiError extends Error {
  readonly status: number
  /** `detail.code` from the API, e.g. `no_license`, `seat_limit_reached`. */
  readonly code: string
  /** Extra fields the API attaches to some codes, e.g. `open_lead_count`. */
  readonly detail: Record<string, unknown>

  constructor(status: number, code: string, message: string, detail: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export interface TokenPair {
  readonly accessToken: string
  readonly refreshToken: string
}

/** Called when refresh fails and the session is unrecoverable. */
type SessionEndedHandler = () => void
/** Called after a successful rotation so the new pair can be persisted. */
type TokensRefreshedHandler = (tokens: TokenPair) => void

let tokens: TokenPair | null = null
let onSessionEnded: SessionEndedHandler = () => {}
let onTokensRefreshed: TokensRefreshedHandler = () => {}
// The single in-flight refresh. Everything that 401s awaits this same promise.
let refreshInFlight: Promise<TokenPair | null> | null = null

export function configureAuth(handlers: {
  onSessionEnded: SessionEndedHandler
  onTokensRefreshed: TokensRefreshedHandler
}): void {
  onSessionEnded = handlers.onSessionEnded
  onTokensRefreshed = handlers.onTokensRefreshed
}

export function setTokens(next: TokenPair | null): void {
  tokens = next
}

export function getAccessToken(): string | null {
  return tokens?.accessToken ?? null
}

interface RequestOptions {
  readonly method?: string
  readonly body?: unknown
  readonly query?: Record<string, string | number | boolean | undefined>
  readonly signal?: AbortSignal
  /** Skips the auth header and the refresh-retry, for `/auth/*` itself. */
  readonly anonymous?: boolean
  /** Sent as-is instead of JSON — used for the members upload. */
  readonly formData?: FormData
}

async function parseError(response: Response, path: string): Promise<ApiError> {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return new ApiError(response.status, 'unknown_error', `Request to ${path} failed`)
  }

  const detail = (payload as { detail?: unknown } | null)?.detail

  // The API's own shape.
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const record = detail as Record<string, unknown>
    return new ApiError(
      response.status,
      typeof record.code === 'string' ? record.code : 'unknown_error',
      typeof record.message === 'string' ? record.message : `Request to ${path} failed`,
      record,
    )
  }

  // FastAPI's request-validation shape: `detail` is an array of field errors.
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: string } | undefined
    return new ApiError(response.status, 'validation_error', first?.msg ?? 'Invalid request')
  }

  return new ApiError(response.status, 'unknown_error', `Request to ${path} failed`)
}

function buildUrl(path: string, query: RequestOptions['query']): string {
  const url = new URL(`${API_V1_URL}${path}`)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) {
      url.searchParams.set(key, String(value))
    }
  }
  return url.toString()
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (!options.anonymous && tokens) {
    headers.Authorization = `Bearer ${tokens.accessToken}`
  }
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  try {
    return await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers,
      ...(options.formData ? { body: options.formData } : {}),
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      ...(options.signal ? { signal: options.signal } : {}),
    })
  } catch (cause) {
    throw new ApiError(
      0,
      'network_error',
      cause instanceof Error ? cause.message : 'Network request failed',
    )
  }
}

/**
 * Rotate the refresh token, coalescing concurrent callers onto one request.
 *
 * The server revokes a refresh token's entire family when a already-rotated
 * token is presented again — that is its stolen-token defence. Two parallel
 * refreshes would look exactly like theft, so they must not happen.
 */
async function refreshTokens(): Promise<TokenPair | null> {
  if (refreshInFlight) {
    return refreshInFlight
  }

  const current = tokens?.refreshToken
  if (!current) {
    return null
  }

  refreshInFlight = (async () => {
    const response = await send('/auth/refresh', {
      method: 'POST',
      body: { refresh_token: current },
      anonymous: true,
    })

    if (!response.ok) {
      return null
    }

    const body = (await response.json()) as { access_token: string; refresh_token: string }
    const next: TokenPair = { accessToken: body.access_token, refreshToken: body.refresh_token }
    setTokens(next)
    onTokensRefreshed(next)
    return next
  })()

  try {
    return await refreshInFlight
  } finally {
    refreshInFlight = null
  }
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await send(path, options)

  if (response.status === 401 && !options.anonymous) {
    const refreshed = await refreshTokens()
    if (!refreshed) {
      setTokens(null)
      onSessionEnded()
      throw await parseError(response, path)
    }
    response = await send(path, options)
  }

  if (!response.ok) {
    throw await parseError(response, path)
  }

  if (response.status === 204) {
    return undefined as T
  }

  try {
    return (await response.json()) as T
  } catch {
    throw new ApiError(response.status, 'invalid_response', `Response from ${path} was not JSON`)
  }
}

/**
 * Fetch a file and hand it to the browser to save.
 *
 * Not a plain navigation: every tenant endpoint needs the `Authorization`
 * header, and a `window.location` assignment sends cookies rather than a
 * bearer token — the request would 401 and the user would see a blank tab.
 *
 * The server's `Content-Disposition` still decides the filename; `fallback` is
 * only used when the header is absent.
 */
/**
 * Fetch a file with the session's token and return it as a blob — for media
 * the browser cannot request itself, since `<audio src>` sends no bearer token.
 */
export async function fetchBlob(path: string): Promise<Blob> {
  let response = await send(path, { method: 'GET' })
  if (response.status === 401) {
    const refreshed = await refreshTokens()
    if (!refreshed) {
      setTokens(null)
      onSessionEnded()
      throw await parseError(response, path)
    }
    response = await send(path, { method: 'GET' })
  }
  if (!response.ok) {
    throw await parseError(response, path)
  }
  return response.blob()
}

export async function downloadFile(path: string, fallback: string): Promise<void> {
  let response = await send(path, { method: 'GET' })
  if (response.status === 401) {
    // Same refresh-and-retry the JSON path gets: a download started just as an
    // access token expired should not dump the user back at the login screen.
    const refreshed = await refreshTokens()
    if (!refreshed) {
      setTokens(null)
      onSessionEnded()
      throw await parseError(response, path)
    }
    response = await send(path, { method: 'GET' })
  }
  if (!response.ok) {
    throw await parseError(response, path)
  }

  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="([^"]+)"/.exec(disposition)
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = match?.[1] ?? fallback
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoked on the next tick: revoking synchronously can race the download in
  // some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'POST', body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'PATCH', body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'PUT', body }),
  delete: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'DELETE' }),
}
