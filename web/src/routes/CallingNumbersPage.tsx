/**
 * Calling numbers (docs/16) — the numbers AI calls go out from.
 *
 * Calls rotate through the list, one number per call, so no single number
 * makes every call. When a number starts getting flagged as spam, replace it
 * here: the next call uses the new list. Empty means Bolna's default number.
 */

import { useEffect, useState } from 'react'
import { Phone, PhoneIncoming } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PageHeader } from '@/components/ui/page-header'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/features/auth/context'
import { formatMoney } from '@/features/wallet/api'
import {
  useCallingNumbers,
  useCancelNumberRequest,
  useFulfilNumberRequest,
  useNumberRequests,
  useRequestNumber,
  useSetCallingNumbers,
  type NumberRequest,
  type NumberRequestStatus,
} from '@/features/voice/callingNumbers'

export function CallingNumbersPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const numbers = useCallingNumbers(workspaceId)
  const save = useSetCallingNumbers(workspaceId)
  const [draft, setDraft] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (numbers.data) setDraft(numbers.data.numbers.join('\n'))
  }, [numbers.data])

  const error =
    save.error instanceof ApiError ? save.error.message : save.error ? 'Could not save.' : null

  return (
    <div className="space-y-5">
      <PageHeader
        title="Calling numbers"
        description="The numbers AI calls go out from. Calls take turns across the list, one number per call."
      />

      {numbers.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : numbers.isError ? (
        <p className="text-muted-foreground text-sm">The calling numbers could not be loaded.</p>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Phone aria-hidden="true" className="size-4" />
              {numbers.data.numbers.length === 0
                ? "Bolna's default number"
                : `${numbers.data.numbers.length} number${numbers.data.numbers.length === 1 ? '' : 's'} in rotation`}
            </CardTitle>
            <CardDescription>
              {numbers.data.editable
                ? 'One number per line. When a number starts being flagged as spam, replace it here — the next call uses the new list.'
                : 'Managed by your account manager. Ask them to add or replace a number.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {numbers.data.editable ? (
              <form
                className="space-y-3"
                onSubmit={(event) => {
                  event.preventDefault()
                  setSaved(false)
                  save.mutate(
                    draft.split('\n').map((line) => line.trim()),
                    { onSuccess: () => setSaved(true) },
                  )
                }}
              >
                <div className="space-y-1.5">
                  <Label htmlFor="calling-numbers">Numbers</Label>
                  <Textarea
                    id="calling-numbers"
                    rows={5}
                    placeholder={'+91 80000 00000\n+91 80000 00001'}
                    value={draft}
                    onChange={(event) => {
                      setDraft(event.target.value)
                      setSaved(false)
                    }}
                    className="font-mono"
                  />
                </div>
                {error ? (
                  <p role="alert" className="text-destructive text-sm">
                    {error}
                  </p>
                ) : null}
                <div className="flex items-center gap-3">
                  <Button type="submit" size="sm" disabled={save.isPending}>
                    {save.isPending ? 'Saving…' : 'Save numbers'}
                  </Button>
                  {saved ? (
                    <span role="status" className="text-muted-foreground text-sm">
                      Saved. The next call uses this list.
                    </span>
                  ) : null}
                </div>
              </form>
            ) : numbers.data.numbers.length > 0 ? (
              <ul className="flex flex-wrap gap-2">
                {numbers.data.numbers.map((number) => (
                  <li key={number}>
                    <Badge variant="outline" className="font-mono">
                      {number}
                    </Badge>
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="text-muted-foreground text-xs">
              Numbers are bought in Bolna (Phone numbers). Each call's page shows the number it went
              out from, under “Called from”.
            </p>
          </CardContent>
        </Card>
      )}

      <YourNumbers workspaceId={workspaceId} />
    </div>
  )
}

const REGIONS = ['No preference', 'Karnataka', 'Maharashtra', 'Delhi NCR', 'Gujarat'] as const

const STATUS: Record<
  NumberRequestStatus,
  { label: string; tone: 'default' | 'outline' | 'destructive' | 'secondary' }
> = {
  PENDING: { label: 'Waiting for a number', tone: 'secondary' },
  ACTIVE: { label: 'Active', tone: 'default' },
  PAST_DUE: { label: 'Payment due', tone: 'destructive' },
  CANCELLED: { label: 'Cancelled', tone: 'outline' },
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'Something went wrong. Please try again.'
}

/**
 * Numbers this workspace rents, and requests waiting for one (docs/16).
 * A client can only ask; the operator buys the number and fulfils it.
 */
function YourNumbers({ workspaceId }: { readonly workspaceId: string }) {
  const requests = useNumberRequests(workspaceId)
  const ask = useRequestNumber(workspaceId)
  const [open, setOpen] = useState(false)
  const [region, setRegion] = useState<string>(REGIONS[0])
  const [note, setNote] = useState('')

  if (requests.isPending) return null
  if (requests.isError) {
    return <p className="text-muted-foreground text-sm">Your numbers could not be loaded.</p>
  }
  const data = requests.data
  const price = formatMoney(data.monthly_price, data.currency)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <PhoneIncoming aria-hidden="true" className="size-4" />
          Your numbers
        </CardTitle>
        <CardDescription>
          A number of your own costs {price} for every 30 days, paid from your wallet. Request one
          and your account manager sets it up; it is added to your calling numbers when it is ready.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {open ? (
          <form
            className="border-border/70 space-y-3 rounded-xl border p-3"
            onSubmit={(event) => {
              event.preventDefault()
              ask.mutate(
                { region: region === REGIONS[0] ? null : region, note: note.trim() || null },
                {
                  onSuccess: () => {
                    setOpen(false)
                    setNote('')
                  },
                },
              )
            }}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="number-region">Preferred region</Label>
                <Select
                  id="number-region"
                  value={region}
                  onChange={(event) => setRegion(event.target.value)}
                >
                  {REGIONS.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="number-note">Note (optional)</Label>
                <Input
                  id="number-note"
                  maxLength={500}
                  placeholder="e.g. for the VE campaign"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
              </div>
            </div>
            <p className="text-muted-foreground text-xs">
              {price} is taken from your wallet now for the first 30 days, then every 30 days while
              you keep the number. If the wallet cannot cover a renewal, the number stops being used
              until it is recharged.
            </p>
            {errorText(ask.error) ? (
              <p role="alert" className="text-destructive text-sm">
                {errorText(ask.error)}
              </p>
            ) : null}
            <div className="flex gap-2">
              <Button type="submit" size="sm" disabled={ask.isPending}>
                {ask.isPending ? 'Requesting…' : `Request and pay ${price}`}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
                Cancel
              </Button>
            </div>
          </form>
        ) : (
          <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
            Request a number
          </Button>
        )}

        {data.items.length === 0 ? (
          <p className="text-muted-foreground text-sm">No numbers yet.</p>
        ) : (
          <ul className="divide-border/60 divide-y">
            {data.items.map((item) => (
              <RequestRow
                key={item.id}
                workspaceId={workspaceId}
                item={item}
                isOperator={data.is_operator}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function RequestRow({
  workspaceId,
  item,
  isOperator,
}: {
  readonly workspaceId: string
  readonly item: NumberRequest
  readonly isOperator: boolean
}) {
  const fulfil = useFulfilNumberRequest(workspaceId)
  const cancel = useCancelNumberRequest(workspaceId)
  const [number, setNumber] = useState('')
  const status = STATUS[item.status]
  const error = errorText(fulfil.error) ?? errorText(cancel.error)

  return (
    <li className="space-y-2 py-3 first:pt-0 last:pb-0" data-testid="number-request">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="font-mono text-sm">{item.phone_number ?? 'Number to be assigned'}</p>
          <p className="text-muted-foreground text-xs">
            {item.region ?? 'No region preference'} ·{' '}
            {formatMoney(item.monthly_price, item.currency)} per 30 days
            {item.paid_until && item.status !== 'CANCELLED'
              ? ` · paid until ${new Date(item.paid_until).toLocaleDateString()}`
              : ''}
            {item.note ? ` · ${item.note}` : ''}
          </p>
        </div>
        <Badge variant={status.tone}>{status.label}</Badge>
      </div>

      {isOperator && item.status === 'PENDING' ? (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (number.trim()) fulfil.mutate({ id: item.id, phoneNumber: number.trim() })
          }}
        >
          <Input
            aria-label="Number bought in Bolna"
            placeholder="Number bought in Bolna"
            className="max-w-56 font-mono"
            value={number}
            onChange={(event) => setNumber(event.target.value)}
          />
          <Button type="submit" size="sm" disabled={fulfil.isPending || !number.trim()}>
            Fulfil
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate(item.id)}
          >
            Cancel and refund
          </Button>
        </form>
      ) : null}
      {isOperator && (item.status === 'ACTIVE' || item.status === 'PAST_DUE') ? (
        <Button
          size="sm"
          variant="ghost"
          disabled={cancel.isPending}
          onClick={() => cancel.mutate(item.id)}
        >
          Release number
        </Button>
      ) : null}
      {error ? (
        <p role="alert" className="text-destructive text-xs">
          {error}
        </p>
      ) : null}
    </li>
  )
}
