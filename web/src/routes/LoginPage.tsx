import { useState } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { AudioLines } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/features/auth/context'

/**
 * Messages for the codes login can answer with.
 *
 * The API deliberately gives the same reply for a wrong password and an
 * unknown address; the copy here must not undo that by being more specific
 * than the server was.
 */
const MESSAGES: Record<string, string> = {
  invalid_credentials: 'Email or password is incorrect.',
  no_license: 'Your account has no licensed seat. Ask a workspace administrator to assign you one.',
  member_inactive:
    'This account has been deactivated. Ask a workspace administrator to restore it.',
  rate_limited: 'Too many attempts. Wait a few minutes and try again.',
  network_error: 'Could not reach the server. Check your connection and try again.',
}

export function LoginPage() {
  const { login, status, isOperator } = useAuth()
  const navigate = useNavigate()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (status === 'authenticated') {
    return <Navigate to={isOperator ? '/operator' : '/'} replace />
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const { isOperator } = await login(email, password)
      // The operator lands in the console, not inside one company (docs/19).
      void navigate(isOperator ? '/operator' : '/', { replace: true })
    } catch (cause) {
      const code = cause instanceof ApiError ? cause.code : 'unknown_error'
      setError(MESSAGES[code] ?? 'Sign in failed. Try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="mb-7 flex flex-col items-center gap-3 text-center">
          <span
            aria-hidden="true"
            className="bg-primary text-primary-foreground shadow-e2 flex size-12 items-center justify-center rounded-2xl"
          >
            <AudioLines className="size-6" />
          </span>
          <span>
            <h1 className="text-xl font-semibold tracking-tight">LevelUp CRM</h1>
            <p className="text-muted-foreground mt-1 text-sm">Sign in to your workspace.</p>
          </span>
        </div>

        <Card className="glass shadow-e2 w-full">
          <CardContent>
            <form
              className="flex flex-col gap-4"
              onSubmit={(event) => void handleSubmit(event)}
              noValidate
            >
              <div className="flex flex-col gap-2">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  name="email"
                  type="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                />
              </div>

              <div className="flex flex-col gap-2">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </div>

              {error ? (
                <p role="alert" className="text-destructive text-sm">
                  {error}
                </p>
              ) : null}

              <Button type="submit" size="lg" disabled={submitting}>
                {submitting ? 'Signing in…' : 'Sign in'}
              </Button>

              {/* Without this the reset flow exists and nobody can reach it —
                and since the workspace model is invite-only, an invitee whose
                link expired has no other way back in. */}
              <Link
                to="/forgot-password"
                className="text-muted-foreground text-center text-xs underline"
              >
                Forgotten your password?
              </Link>
            </form>
          </CardContent>
        </Card>
      </div>
    </main>
  )
}
