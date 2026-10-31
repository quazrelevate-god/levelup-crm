/**
 * The console's smaller screens (docs/19): calling-number requests from every
 * company, the full history, and the operator logins.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  type SignIn,
  useNumberRequests,
  useOperatorAccounts,
  useOperatorLink,
  useSettleNumber,
} from '@/features/operator/api'
import { HistoryList } from '@/features/operator/HistoryList'
import { money } from '@/features/operator/roleInfo'
import { SignInHandover } from '@/features/operator/ui'

export function NumberRequestsPage() {
  const requests = useNumberRequests()
  const settle = useSettleNumber()
  const [numbers, setNumbers] = useState<Record<string, string>>({})

  return (
    <>
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Number requests</h1>
        <p className="text-muted-foreground text-sm">
          Companies asking for a calling number. Buy it in Bolna, then enter it here — the first
          month is already paid from their wallet. Cancelling refunds it.
        </p>
      </div>
      {settle.error ? (
        <p role="alert" className="text-destructive text-sm">
          {settle.error instanceof ApiError ? settle.error.message : 'That did not work.'}
        </p>
      ) : null}
      {requests.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : (requests.data ?? []).length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing waiting.</p>
      ) : (
        <div className="space-y-2">
          {(requests.data ?? []).map((r) => (
            <Card key={r.id}>
              <CardContent className="flex flex-wrap items-center gap-3 pt-5">
                <span className="min-w-0">
                  <Link
                    to={`/operator/companies/${r.workspace_id}`}
                    className="font-medium hover:underline"
                  >
                    {r.company}
                  </Link>
                  <span className="text-muted-foreground block text-xs">
                    {r.region ?? 'Any region'} · {money(r.monthly_price, 'INR')}/month · asked{' '}
                    {new Date(r.created_at).toLocaleDateString()}
                  </span>
                </span>
                <Input
                  aria-label={`Number for ${r.company}`}
                  placeholder="+91…"
                  className="ml-auto max-w-44"
                  value={numbers[r.id] ?? ''}
                  onChange={(e) => setNumbers({ ...numbers, [r.id]: e.target.value })}
                />
                <Button
                  disabled={!numbers[r.id]?.trim() || settle.isPending}
                  onClick={() =>
                    settle.mutate({
                      workspaceId: r.workspace_id,
                      requestId: r.id,
                      phone: numbers[r.id]?.trim() ?? null,
                    })
                  }
                >
                  Give number
                </Button>
                <Button
                  variant="ghost"
                  disabled={settle.isPending}
                  onClick={() => {
                    if (window.confirm(`Cancel ${r.company}'s request and refund the month?`)) {
                      settle.mutate({ workspaceId: r.workspace_id, requestId: r.id, phone: null })
                    }
                  }}
                >
                  Cancel
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  )
}

export function OperatorHistoryPage() {
  return (
    <>
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">History</h1>
        <p className="text-muted-foreground text-sm">
          Everything done from this console, in every company — who, what and when.
        </p>
      </div>
      <HistoryList workspaceId={null} />
    </>
  )
}

export function OperatorLoginsPage() {
  const accounts = useOperatorAccounts()
  const link = useOperatorLink()
  const [shown, setShown] = useState<{ email: string; signIn: SignIn } | null>(null)

  return (
    <>
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Operator logins</h1>
        <p className="text-muted-foreground text-sm">
          The addresses allowed into this console. The list itself is set on the server, so nobody
          inside the CRM can add themselves.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Addresses</CardTitle>
          <CardDescription>
            Get a link for an address to set (or reset) its password. Open it in a private window,
            choose the password, then sign in.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {(accounts.data ?? []).map((a) => (
            <div
              key={a.email}
              className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2"
            >
              <span className="font-medium">{a.email}</span>
              {a.has_account ? (
                <Badge variant="success">Can sign in</Badge>
              ) : (
                <Badge variant="outline">No password yet</Badge>
              )}
              <Button
                size="sm"
                variant="outline"
                className="ml-auto"
                disabled={link.isPending}
                onClick={() =>
                  link.mutate(a.email, {
                    onSuccess: (signIn) => setShown({ email: a.email, signIn }),
                  })
                }
              >
                {a.has_account ? 'Reset password link' : 'Set password link'}
              </Button>
            </div>
          ))}
        </CardContent>
      </Card>
      {shown ? (
        <Dialog open onClose={() => setShown(null)} title={`Link for ${shown.email}`}>
          <SignInHandover name="you" email={shown.email} signIn={shown.signIn} />
        </Dialog>
      ) : null}
    </>
  )
}
