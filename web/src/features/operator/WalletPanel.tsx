/**
 * A company's wallet in the console (docs/19): the balance, the price per AI
 * minute, and every movement. Money goes in through a two-step form — type,
 * then confirm what the balance will become — because a mistyped zero is the
 * most expensive typo there is. Nothing is ever deleted; a mistake is fixed
 * with a correction that shows in the history.
 */

import { useState } from 'react'
import { Gift, PencilLine, Plus } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

import { type Company, useCompanyWallet, useMoney, useSetRate } from './api'
import { money } from './roleInfo'

type Kind = 'TOPUP' | 'DISCOUNT' | 'ADJUSTMENT'

const KINDS: Record<Kind, { title: string; hint: string; button: string }> = {
  TOPUP: {
    title: 'Add money',
    hint: 'They paid you. Write how (UPI ref, invoice no.) so you can find it later.',
    button: 'Add money',
  },
  DISCOUNT: {
    title: 'Give free credit',
    hint: 'Credit without a payment — a goodwill discount. Kept apart from real payments.',
    button: 'Give credit',
  },
  ADJUSTMENT: {
    title: 'Correct the balance',
    hint: 'Fix a mistake. Use a minus sign to take money back, e.g. -500. A reason is required.',
    button: 'Correct',
  },
}

const LABELS: Record<string, string> = {
  TOPUP: 'Money added',
  DISCOUNT: 'Free credit',
  ADJUSTMENT: 'Correction',
  CALL_CHARGE: 'AI call',
  NUMBER_RENTAL: 'Calling number',
}

export function WalletPanel({ company }: { readonly company: Company }) {
  const [offset, setOffset] = useState(0)
  const wallet = useCompanyWallet(company.id, offset)
  const setRate = useSetRate(company.id)
  const [kind, setKind] = useState<Kind | null>(null)
  const [rate, setRateDraft] = useState('')
  const w = wallet.data
  const low = w && w.balance !== null && Number(w.balance) < Number(w.low_balance_at)

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-[1fr_1fr]">
        <Card className={cn(low && 'border-amber-500/60')}>
          <CardContent className="space-y-3 pt-5">
            <p className="text-muted-foreground text-xs">Balance</p>
            <p className="text-3xl font-semibold tabular-nums">
              {money(w?.balance ?? null, company.currency)}
            </p>
            {low ? (
              <Badge variant="destructive">
                Low — below {money(w.low_balance_at, company.currency)}
              </Badge>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => setKind('TOPUP')}>
                <Plus aria-hidden="true" /> Add money
              </Button>
              <Button variant="outline" onClick={() => setKind('DISCOUNT')}>
                <Gift aria-hidden="true" /> Free credit
              </Button>
              <Button variant="outline" onClick={() => setKind('ADJUSTMENT')}>
                <PencilLine aria-hidden="true" /> Correct
              </Button>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="space-y-3 pt-5">
            <p className="text-muted-foreground text-xs">Price per connected AI-call minute</p>
            <p className="text-3xl font-semibold tabular-nums">
              {money(w?.rate_per_minute ?? null, company.currency)}
            </p>
            <form
              className="flex gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                setRate.mutate(rate, { onSuccess: () => setRateDraft('') })
              }}
            >
              <Input
                type="number"
                min={0}
                step="0.01"
                aria-label="New price per minute"
                placeholder="New price"
                className="max-w-36"
                value={rate}
                onChange={(e) => setRateDraft(e.target.value)}
              />
              <Button type="submit" variant="outline" disabled={!rate || setRate.isPending}>
                Change price
              </Button>
            </form>
            <p className="text-muted-foreground text-xs">Applies to calls from now on.</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="space-y-3 pt-5">
          <p className="font-semibold">Money history</p>
          {!w || w.entries.length === 0 ? (
            <p className="text-muted-foreground text-sm">Nothing yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-muted-foreground border-b text-left text-xs">
                  <tr>
                    <th className="py-2 pr-3 font-medium">When</th>
                    <th className="py-2 pr-3 font-medium">What</th>
                    <th className="py-2 pr-3 text-right font-medium">Amount</th>
                    <th className="py-2 pr-3 text-right font-medium">Balance after</th>
                    <th className="py-2 pr-3 font-medium">Note</th>
                    <th className="py-2 font-medium">By</th>
                  </tr>
                </thead>
                <tbody>
                  {w.entries.map((e) => (
                    <tr key={e.id} className="border-border/60 border-b last:border-b-0">
                      <td className="py-2 pr-3 whitespace-nowrap">
                        {new Date(e.created_at).toLocaleString()}
                      </td>
                      <td className="py-2 pr-3">
                        {LABELS[e.kind] ?? e.kind}
                        {e.billed_minutes ? (
                          <span className="text-muted-foreground"> · {e.billed_minutes} min</span>
                        ) : null}
                      </td>
                      <td
                        className={cn(
                          'py-2 pr-3 text-right tabular-nums',
                          Number(e.amount) < 0 ? 'text-destructive' : 'text-emerald-600',
                        )}
                      >
                        {Number(e.amount) > 0 ? '+' : ''}
                        {money(e.amount, company.currency)}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">
                        {money(e.balance_after, company.currency)}
                      </td>
                      <td className="text-muted-foreground py-2 pr-3">{e.note ?? ''}</td>
                      <td className="text-muted-foreground py-2 text-xs">
                        {e.recorded_by ?? 'CRM'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {w && w.total_entries > 20 ? (
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - 20))}
              >
                Newer
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={offset + 20 >= w.total_entries}
                onClick={() => setOffset(offset + 20)}
              >
                Older
              </Button>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {kind ? (
        <MoneyDialog
          company={company}
          kind={kind}
          balance={w?.balance ?? '0'}
          onClose={() => setKind(null)}
        />
      ) : null}
    </div>
  )
}

function MoneyDialog({
  company,
  kind,
  balance,
  onClose,
}: {
  readonly company: Company
  readonly kind: Kind
  readonly balance: string
  readonly onClose: () => void
}) {
  const save = useMoney(company.id)
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [confirming, setConfirming] = useState(false)
  const spec = KINDS[kind]
  const value = Number(amount)
  const valid =
    amount !== '' &&
    !Number.isNaN(value) &&
    (kind === 'ADJUSTMENT' ? value !== 0 && note.trim() !== '' : value > 0)
  const after = Number(balance) + (Number.isNaN(value) ? 0 : value)

  return (
    <Dialog
      open
      onClose={onClose}
      title={`${spec.title} — ${company.name}`}
      description={confirming ? 'Check before it is recorded. This cannot be deleted.' : spec.hint}
      footer={
        <>
          <Button variant="ghost" onClick={confirming ? () => setConfirming(false) : onClose}>
            {confirming ? 'Back' : 'Cancel'}
          </Button>
          {confirming ? (
            <Button
              disabled={save.isPending}
              onClick={() =>
                save.mutate(
                  { kind, amount: String(value), note: note.trim() },
                  { onSuccess: onClose },
                )
              }
            >
              {save.isPending ? 'Recording…' : `Yes, ${spec.button.toLowerCase()}`}
            </Button>
          ) : (
            <Button disabled={!valid} onClick={() => setConfirming(true)}>
              Next
            </Button>
          )}
        </>
      }
    >
      {confirming ? (
        <div className="space-y-2 text-sm">
          <p>
            {spec.title}:{' '}
            <b className={cn(value < 0 ? 'text-destructive' : 'text-emerald-600')}>
              {value > 0 ? '+' : ''}
              {money(String(value), company.currency)}
            </b>
          </p>
          <p>
            Balance {money(balance, company.currency)} →{' '}
            <b>{money(String(after), company.currency)}</b>
          </p>
          {note.trim() ? <p className="text-muted-foreground">Note: {note.trim()}</p> : null}
          {save.error ? (
            <p role="alert" className="text-destructive">
              {save.error instanceof ApiError ? save.error.message : 'It could not be recorded.'}
            </p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-3">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Amount ({company.currency})</span>
            <Input
              autoFocus
              type="number"
              step="0.01"
              value={amount}
              placeholder={kind === 'ADJUSTMENT' ? 'e.g. -500 or 200' : 'e.g. 5000'}
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">
              Note {kind === 'ADJUSTMENT' ? '(required)' : '(optional)'}
            </span>
            <Input
              value={note}
              maxLength={500}
              placeholder={kind === 'TOPUP' ? 'e.g. UPI ref 4512…' : 'Why'}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
        </div>
      )}
    </Dialog>
  )
}
