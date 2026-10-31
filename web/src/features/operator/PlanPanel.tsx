/**
 * What a company bought (docs/19): its name and region, its seats, and which
 * modules are switched on. Plus the one serious switch — suspending it — kept
 * at the bottom with its own confirmation.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'

import { type Company, useFeatures, useUpdateCompany } from './api'

export function PlanPanel({ company }: { readonly company: Company }) {
  const features = useFeatures()
  const update = useUpdateCompany(company.id)
  const [name, setName] = useState(company.name)
  const [seats, setSeats] = useState(String(company.seat_limit))
  const [suspendOpen, setSuspendOpen] = useState(false)
  const error =
    update.error instanceof ApiError
      ? update.error.message
      : update.error
        ? 'That did not work.'
        : null

  return (
    <div className="space-y-4">
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle>Company and seats</CardTitle>
          <CardDescription>
            {company.seats_used} of {company.seat_limit} seats in use. You can’t go below the number
            in use — switch someone off first.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault()
              update.mutate({
                ...(name.trim() !== company.name ? { name: name.trim() } : {}),
                ...(Number(seats) !== company.seat_limit ? { seat_limit: Number(seats) } : {}),
              })
            }}
          >
            <label className="block space-y-1.5">
              <span className="text-sm font-medium">Name</span>
              <Input value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="block space-y-1.5">
              <span className="text-sm font-medium">Seats</span>
              <Input
                type="number"
                min={1}
                className="w-28"
                value={seats}
                onChange={(e) => setSeats(e.target.value)}
              />
            </label>
            <Button
              type="submit"
              disabled={
                update.isPending ||
                (name.trim() === company.name && Number(seats) === company.seat_limit) ||
                !name.trim() ||
                Number(seats) < 1
              }
            >
              Save
            </Button>
          </form>
          <p className="text-muted-foreground mt-3 text-xs">
            +{company.default_country_code} · {company.timezone} · {company.currency}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Features</CardTitle>
          <CardDescription>
            Switching one off hides it and blocks it for everyone in the company.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 sm:grid-cols-2">
          {(features.data ?? []).map((f) => (
            <label
              key={f.key}
              className="flex cursor-pointer items-start gap-3 rounded-lg border p-3"
            >
              <Switch
                aria-label={f.label}
                disabled={update.isPending}
                checked={Boolean(company.features[f.key])}
                onCheckedChange={(on) => update.mutate({ features: { [f.key]: on } })}
              />
              <span className="text-sm">
                <span className="block font-medium">{f.label}</span>
                <span className="text-muted-foreground text-xs">{f.description}</span>
              </span>
            </label>
          ))}
        </CardContent>
      </Card>

      <Card className={company.is_active ? 'border-destructive/40' : 'border-emerald-500/40'}>
        <CardHeader>
          <CardTitle>
            {company.is_active ? 'Suspend this company' : 'This company is suspended'}
          </CardTitle>
          <CardDescription>
            {company.is_active
              ? 'Nobody in it can sign in and no AI calls go out. Nothing is deleted; reactivate any time.'
              : 'Its people cannot sign in. Reactivate to give them access again — everything is as they left it.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {company.is_active ? (
            <Button variant="destructive" onClick={() => setSuspendOpen(true)}>
              Suspend {company.name}
            </Button>
          ) : (
            <Button disabled={update.isPending} onClick={() => update.mutate({ is_active: true })}>
              Reactivate {company.name}
            </Button>
          )}
        </CardContent>
      </Card>

      {suspendOpen ? (
        <Dialog
          open
          onClose={() => setSuspendOpen(false)}
          title={`Suspend ${company.name}?`}
          description="Everyone in this company is locked out until you reactivate it."
          footer={
            <>
              <Button variant="ghost" onClick={() => setSuspendOpen(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={update.isPending}
                onClick={() =>
                  update.mutate({ is_active: false }, { onSuccess: () => setSuspendOpen(false) })
                }
              >
                Yes, suspend
              </Button>
            </>
          }
        >
          <p className="text-sm">
            {company.seats_used} people will not be able to sign in. Their leads, calls and wallet
            stay exactly as they are.
          </p>
        </Dialog>
      ) : null}
    </div>
  )
}
