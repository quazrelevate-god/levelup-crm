/**
 * Small pieces the console screens share: the hierarchy strip, a role chip,
 * and the box that hands over a sign-in link.
 */

import { useState } from 'react'
import { Building2, Check, ChevronRight, Copy, Crown, MessageCircle } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

import type { SignIn } from './api'
import { LEVELS, roleLook } from './roleInfo'

export function RoleChip({
  name,
  className,
}: {
  readonly name: string
  readonly className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold',
        roleLook(name).chip,
        className,
      )}
    >
      {name}
    </span>
  )
}

/**
 * Operator → Company → Root → Admin → Manager → Caller, left to right, with
 * the number of people at each level. `highlight` marks where the operator is.
 */
export function HierarchyStrip({
  company,
  counts,
  highlight,
}: {
  readonly company?: string
  readonly counts?: Readonly<Record<string, number>>
  readonly highlight?: string
}) {
  const steps: { key: string; label: string; sub?: string; className: string }[] = [
    {
      key: 'Operator',
      label: 'Operator',
      sub: 'you',
      className: 'bg-slate-900 text-white dark:bg-slate-700',
    },
    {
      key: 'Company',
      label: company ?? 'Company',
      sub: 'client',
      className: 'bg-primary text-primary-foreground',
    },
    ...LEVELS.map((level) => ({
      key: level,
      label: level,
      sub: counts ? `${counts[level] ?? 0}` : undefined,
      className: roleLook(level).chip,
    })),
  ]
  return (
    <ol className="flex flex-wrap items-center gap-1.5" aria-label="Hierarchy">
      {steps.map((step, index) => (
        <li key={step.key} className="flex items-center gap-1.5">
          <span
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-semibold',
              step.className,
              highlight && highlight !== step.key && 'opacity-60',
            )}
          >
            {step.key === 'Operator' ? <Crown aria-hidden="true" className="size-3.5" /> : null}
            {step.key === 'Company' ? <Building2 aria-hidden="true" className="size-3.5" /> : null}
            <span className="max-w-40 truncate">{step.label}</span>
            {step.sub ? <span className="font-normal opacity-80">· {step.sub}</span> : null}
          </span>
          {index < steps.length - 1 ? (
            <ChevronRight aria-hidden="true" className="text-muted-foreground size-3.5" />
          ) : null}
        </li>
      ))}
    </ol>
  )
}

/** The "send this to them" box after creating a person. */
export function SignInHandover({
  name,
  email,
  signIn,
}: {
  readonly name: string
  readonly email: string
  readonly signIn: SignIn
}) {
  const [copied, setCopied] = useState(false)
  if (!signIn.link) {
    return (
      <div className="bg-muted/40 rounded-xl border p-4 text-sm">
        <p className="font-medium">{name} already has an account.</p>
        <p className="text-muted-foreground">
          They sign in at the CRM with {email} and their own password, and will see this company.
        </p>
      </div>
    )
  }
  const message = `Hi ${name}, your CRM account is ready. Set your password here (link works once): ${signIn.link}`
  return (
    <div className="space-y-3 rounded-xl border border-emerald-500/40 bg-emerald-500/5 p-4">
      <p className="text-sm font-medium">
        Send this link to {name} ({email}). They open it and choose their own password.
      </p>
      <div className="flex gap-2">
        <Input
          readOnly
          aria-label="Sign-in link"
          value={signIn.link}
          className="font-mono text-xs"
        />
        <Button
          variant="outline"
          onClick={() =>
            void navigator.clipboard.writeText(signIn.link ?? '').then(() => {
              setCopied(true)
              window.setTimeout(() => setCopied(false), 2000)
            })
          }
        >
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
        <Button asChild variant="outline">
          <a
            href={`https://wa.me/?text=${encodeURIComponent(message)}`}
            target="_blank"
            rel="noreferrer"
          >
            <MessageCircle aria-hidden="true" /> WhatsApp
          </a>
        </Button>
      </div>
      {signIn.expires_at ? (
        <p className="text-muted-foreground text-xs">
          Works once, until {new Date(signIn.expires_at).toLocaleString()}. You can make a new one
          any time from their row.
        </p>
      ) : null}
    </div>
  )
}
