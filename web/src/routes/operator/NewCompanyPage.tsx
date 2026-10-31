/**
 * New company, as a guided walk (docs/19): the company → what they bought →
 * their wallet → their owner (Root) → check → done, with the owner's sign-in
 * link to send. One question per step, so nothing is forgotten.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, ArrowRight, Check } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import {
  type Company,
  type NewCompany,
  type SignIn,
  useCreateCompany,
  useFeatures,
} from '@/features/operator/api'
import { money } from '@/features/operator/roleInfo'
import { HierarchyStrip, RoleChip, SignInHandover } from '@/features/operator/ui'
import { cn } from '@/lib/utils'

const STEPS = ['Company', 'Seats & features', 'Wallet', 'Owner (Root)', 'Check & create'] as const

const TIMEZONES = [
  'Asia/Kolkata',
  'Asia/Dubai',
  'Asia/Singapore',
  'Europe/London',
  'America/New_York',
  'America/Los_Angeles',
  'Australia/Sydney',
]

export function NewCompanyPage() {
  const features = useFeatures()
  const create = useCreateCompany()
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [done, setDone] = useState<{ company: Company; owner: SignIn } | null>(null)
  const [draft, setDraft] = useState<NewCompany>({
    name: '',
    default_country_code: '91',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    seat_limit: 5,
    features: {},
    opening_balance: '0',
    rate_per_minute: '5',
    owner_name: '',
    owner_email: '',
  })
  const set = (patch: Partial<NewCompany>) => setDraft({ ...draft, ...patch })
  const featureOn = (key: string, fallback: boolean) => draft.features[key] ?? fallback

  const stepReady = [
    draft.name.trim().length > 0,
    draft.seat_limit >= 1,
    Number(draft.opening_balance) >= 0 && Number(draft.rate_per_minute) >= 0,
    draft.owner_name.trim().length > 0 && /.+@.+\..+/.test(draft.owner_email),
    true,
  ]

  if (done) {
    return (
      <div className="mx-auto max-w-2xl space-y-5">
        <HierarchyStrip company={done.company.name} highlight="Root" counts={{ Root: 1 }} />
        <Card>
          <CardContent className="space-y-4 pt-6">
            <p className="flex items-center gap-2 text-lg font-semibold">
              <Check aria-hidden="true" className="size-5 text-emerald-600" />
              {done.company.name} is ready
            </p>
            <p className="text-muted-foreground text-sm">
              {done.company.seat_limit} seats · wallet{' '}
              {money(done.company.balance, done.company.currency)}
              {' · '}owner {done.company.owner_name} <RoleChip name="Root" />
            </p>
            <SignInHandover
              name={done.company.owner_name ?? 'the owner'}
              email={done.company.owner_email ?? ''}
              signIn={done.owner}
            />
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() => void navigate(`/operator/companies/${done.company.id}?tab=people`)}
              >
                Add their team <ArrowRight aria-hidden="true" />
              </Button>
              <Button variant="outline" asChild>
                <Link to="/operator">Back to companies</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div className="space-y-2">
        <Link to="/operator" className="text-muted-foreground flex items-center gap-1 text-sm">
          <ArrowLeft aria-hidden="true" className="size-4" /> Companies
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">New company</h1>
        <HierarchyStrip company={draft.name || 'New company'} highlight="Company" />
      </div>

      <ol className="grid grid-cols-5 gap-2" aria-label="Steps">
        {STEPS.map((label, index) => (
          <li key={label}>
            <button
              type="button"
              disabled={index > step && !stepReady.slice(0, index).every(Boolean)}
              onClick={() => setStep(index)}
              className={cn(
                'w-full rounded-lg border px-2 py-2 text-left text-xs disabled:opacity-50',
                index === step && 'border-primary bg-primary/10 font-semibold',
                index < step && 'border-emerald-500/50',
              )}
            >
              <span className="text-muted-foreground block">Step {index + 1}</span>
              {label}
            </button>
          </li>
        ))}
      </ol>

      <Card>
        <CardContent className="space-y-4 pt-6">
          {step === 0 ? (
            <>
              <Field label="Company name" hint="As your client calls themselves.">
                <Input
                  autoFocus
                  value={draft.name}
                  maxLength={200}
                  placeholder="e.g. Acme Academy"
                  onChange={(e) => set({ name: e.target.value })}
                />
              </Field>
              <div className="grid gap-4 sm:grid-cols-3">
                <Field label="Country code" hint="For their leads' phone numbers.">
                  <Input
                    value={draft.default_country_code}
                    onChange={(e) =>
                      set({ default_country_code: e.target.value.replace(/\D/g, '') })
                    }
                  />
                </Field>
                <Field label="Timezone">
                  <Select
                    value={draft.timezone}
                    onChange={(e) => set({ timezone: e.target.value })}
                  >
                    {TIMEZONES.map((tz) => (
                      <option key={tz}>{tz}</option>
                    ))}
                  </Select>
                </Field>
                <Field label="Currency">
                  <Select
                    value={draft.currency}
                    onChange={(e) => set({ currency: e.target.value })}
                  >
                    {['INR', 'USD', 'AED', 'GBP', 'SGD', 'AUD'].map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </Select>
                </Field>
              </div>
            </>
          ) : null}

          {step === 1 ? (
            <>
              <Field
                label="How many people can use it (seats)"
                hint="Everyone with a login takes a seat, the owner included. You can change this later."
              >
                <Input
                  type="number"
                  min={1}
                  max={10000}
                  className="max-w-32"
                  value={draft.seat_limit}
                  onChange={(e) => set({ seat_limit: Math.max(1, Number(e.target.value) || 1) })}
                />
              </Field>
              <div className="space-y-2">
                <p className="text-sm font-medium">Features they get</p>
                <div className="grid gap-2 sm:grid-cols-2">
                  {(features.data ?? []).map((f) => (
                    <label
                      key={f.key}
                      className="hover:bg-muted/40 flex cursor-pointer items-start gap-3 rounded-lg border p-3"
                    >
                      <Switch
                        aria-label={f.label}
                        checked={featureOn(f.key, f.on_by_default)}
                        onCheckedChange={(on) =>
                          set({ features: { ...draft.features, [f.key]: on } })
                        }
                      />
                      <span className="text-sm">
                        <span className="block font-medium">{f.label}</span>
                        <span className="text-muted-foreground text-xs">{f.description}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            </>
          ) : null}

          {step === 2 ? (
            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                label={`Opening balance (${draft.currency})`}
                hint="Money they have already paid you. 0 if none yet — you can add it later."
              >
                <Input
                  type="number"
                  min={0}
                  step="0.01"
                  value={draft.opening_balance}
                  onChange={(e) => set({ opening_balance: e.target.value })}
                />
              </Field>
              <Field
                label={`Price per AI-call minute (${draft.currency})`}
                hint="Taken from their wallet for every connected minute."
              >
                <Input
                  type="number"
                  min={0}
                  step="0.01"
                  value={draft.rate_per_minute}
                  onChange={(e) => set({ rate_per_minute: e.target.value })}
                />
              </Field>
            </div>
          ) : null}

          {step === 3 ? (
            <>
              <div className="bg-muted/40 flex items-start gap-3 rounded-lg p-3 text-sm">
                <RoleChip name="Root" />
                <span>
                  The owner gets <b>Root</b>: full control of their company, and a role that can
                  never be locked out. You can add Admins, Managers and Callers after this.
                </span>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Owner's full name">
                  <Input
                    value={draft.owner_name}
                    onChange={(e) => set({ owner_name: e.target.value })}
                  />
                </Field>
                <Field label="Owner's email" hint="Their login. They set their own password.">
                  <Input
                    type="email"
                    value={draft.owner_email}
                    onChange={(e) => set({ owner_email: e.target.value })}
                  />
                </Field>
              </div>
            </>
          ) : null}

          {step === 4 ? (
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
              <Row label="Company" value={draft.name} />
              <Row
                label="Region"
                value={`+${draft.default_country_code} · ${draft.timezone} · ${draft.currency}`}
              />
              <Row label="Seats" value={String(draft.seat_limit)} />
              <Row
                label="Features on"
                value={
                  (features.data ?? [])
                    .filter((f) => featureOn(f.key, f.on_by_default))
                    .map((f) => f.label)
                    .join(', ') || 'None'
                }
              />
              <Row label="Opening balance" value={money(draft.opening_balance, draft.currency)} />
              <Row label="Per AI minute" value={money(draft.rate_per_minute, draft.currency)} />
              <Row label="Owner (Root)" value={`${draft.owner_name} — ${draft.owner_email}`} />
            </dl>
          ) : null}

          {create.error ? (
            <p role="alert" className="text-destructive text-sm">
              {create.error instanceof ApiError ? create.error.message : 'It could not be created.'}
            </p>
          ) : null}

          <div className="flex justify-between gap-2 border-t pt-4">
            <Button variant="ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>
              <ArrowLeft aria-hidden="true" /> Back
            </Button>
            {step < STEPS.length - 1 ? (
              <Button disabled={!stepReady[step]} onClick={() => setStep(step + 1)}>
                Next: {STEPS[step + 1]} <ArrowRight aria-hidden="true" />
              </Button>
            ) : (
              <Button
                disabled={create.isPending}
                onClick={() =>
                  create.mutate(
                    {
                      ...draft,
                      features: Object.fromEntries(
                        (features.data ?? []).map((f) => [
                          f.key,
                          featureOn(f.key, f.on_by_default),
                        ]),
                      ),
                    },
                    { onSuccess: setDone },
                  )
                }
              >
                {create.isPending ? 'Creating…' : 'Create company'}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  readonly label: string
  readonly hint?: string
  readonly children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      {/* Wrapping the control names it, without an id per field. */}
      <label className="block space-y-1.5">
        <span className="text-sm font-medium">{label}</span>
        {children}
      </label>
      {hint ? <p className="text-muted-foreground text-xs">{hint}</p> : null}
    </div>
  )
}

function Row({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  )
}
