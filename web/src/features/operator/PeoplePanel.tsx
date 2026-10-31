/**
 * A company's people as a tree (docs/19): who reports to whom, each person's
 * role in its own colour, and a guided way to add anyone at any level.
 */

import { useMemo, useState } from 'react'
import { ArrowLeft, ArrowRight, KeyRound, Pencil, UserPlus } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'

import {
  type Company,
  type Person,
  type Role,
  type SignIn,
  useAddPerson,
  usePeople,
  useRoles,
  useSignInLink,
  useUpdatePerson,
} from './api'
import { roleLook } from './roleInfo'
import { HierarchyStrip, RoleChip, SignInHandover } from './ui'

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'That did not work. Please try again.'
}

interface TreeNode {
  readonly person: Person
  readonly children: TreeNode[]
}

function buildTree(people: readonly Person[]): TreeNode[] {
  const ids = new Set(people.map((p) => p.membership_id))
  const byManager = new Map<string | null, Person[]>()
  for (const person of people) {
    const key = person.manager_id && ids.has(person.manager_id) ? person.manager_id : null
    byManager.set(key, [...(byManager.get(key) ?? []), person])
  }
  const order = (a: Person, b: Person) =>
    roleLook(a.role_name).rank - roleLook(b.role_name).rank ||
    a.full_name.localeCompare(b.full_name)
  const grow = (key: string | null, seen: Set<string>): TreeNode[] =>
    (byManager.get(key) ?? [])
      .filter((p) => !seen.has(p.membership_id))
      .sort(order)
      .map((person) => ({
        person,
        children: grow(person.membership_id, new Set([...seen, person.membership_id])),
      }))
  return grow(null, new Set())
}

export function PeoplePanel({ company }: { readonly company: Company }) {
  const people = usePeople(company.id)
  const roles = useRoles(company.id)
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<Person | null>(null)
  const [linkFor, setLinkFor] = useState<{ person: Person; signIn: SignIn } | null>(null)
  const signInLink = useSignInLink(company.id)

  const team = useMemo(() => (people.data ?? []).filter((p) => !p.is_support), [people.data])
  const counts = useMemo(() => {
    const out: Record<string, number> = {}
    for (const p of team) if (p.is_active) out[p.role_name] = (out[p.role_name] ?? 0) + 1
    return out
  }, [team])
  const tree = useMemo(() => buildTree(team), [team])
  const seatsFull = company.seats_used >= company.seat_limit

  const renderNode = (node: TreeNode, depth: number) => {
    const p = node.person
    return (
      <li key={p.membership_id}>
        <div
          className={cn(
            'hover:bg-muted/40 flex flex-wrap items-center gap-3 rounded-lg px-3 py-2',
            !p.is_active && 'opacity-50',
          )}
        >
          <span className={cn('size-2.5 shrink-0 rounded-full', roleLook(p.role_name).dot)} />
          <span className="min-w-0">
            <span className="block font-medium">{p.full_name}</span>
            <span className="text-muted-foreground block text-xs">{p.email}</span>
          </span>
          <RoleChip name={p.role_name} />
          {!p.is_active ? <Badge variant="outline">Switched off</Badge> : null}
          <span className="ml-auto flex gap-1">
            <Button
              size="sm"
              variant="ghost"
              disabled={signInLink.isPending}
              onClick={() =>
                signInLink.mutate(p.membership_id, {
                  onSuccess: (signIn) => setLinkFor({ person: p, signIn }),
                })
              }
            >
              <KeyRound aria-hidden="true" /> Sign-in link
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(p)}>
              <Pencil aria-hidden="true" /> Edit
            </Button>
          </span>
        </div>
        {node.children.length > 0 ? (
          <ul className="border-border ml-4 border-l pl-3">
            {node.children.map((child) => renderNode(child, depth + 1))}
          </ul>
        ) : null}
      </li>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <HierarchyStrip company={company.name} counts={counts} />
        <Button disabled={seatsFull} onClick={() => setAdding(true)}>
          <UserPlus aria-hidden="true" /> Add person
        </Button>
      </div>
      {seatsFull ? (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          All {company.seat_limit} seats are used. Raise the seats in “Plan & features” to add
          someone.
        </p>
      ) : null}
      {people.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : (
        <ul className="space-y-0.5">{tree.map((node) => renderNode(node, 0))}</ul>
      )}
      <p className="text-muted-foreground text-xs">
        Lines show who reports to whom. A manager sees their own leads and their team’s.
      </p>

      {adding && roles.data ? (
        <AddPersonDialog
          company={company}
          roles={roles.data}
          people={team}
          onClose={() => setAdding(false)}
        />
      ) : null}
      {editing && roles.data ? (
        <EditPersonDialog
          company={company}
          person={editing}
          roles={roles.data}
          people={team}
          onClose={() => setEditing(null)}
        />
      ) : null}
      {linkFor ? (
        <Dialog
          open
          onClose={() => setLinkFor(null)}
          title={`Sign-in link for ${linkFor.person.full_name}`}
        >
          <SignInHandover
            name={linkFor.person.full_name}
            email={linkFor.person.email}
            signIn={linkFor.signIn}
          />
        </Dialog>
      ) : null}
    </div>
  )
}

function sortedRoles(roles: readonly Role[]): Role[] {
  return [...roles].sort((a, b) => roleLook(a.name).rank - roleLook(b.name).rank)
}

/** Who can sit above someone of this role: anyone ranked higher, active. */
function managersFor(
  role: Role | undefined,
  people: readonly Person[],
  exclude?: string,
): Person[] {
  const rank = role ? roleLook(role.name).rank : 99
  return people.filter(
    (p) => p.is_active && p.membership_id !== exclude && roleLook(p.role_name).rank < rank,
  )
}

function AddPersonDialog({
  company,
  roles,
  people,
  onClose,
}: {
  readonly company: Company
  readonly roles: readonly Role[]
  readonly people: readonly Person[]
  readonly onClose: () => void
}) {
  const add = useAddPerson(company.id)
  const [step, setStep] = useState(0)
  const [roleId, setRoleId] = useState<string | null>(null)
  const [managerId, setManagerId] = useState<string>('')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [result, setResult] = useState<{ person: Person; sign_in: SignIn } | null>(null)
  const role = roles.find((r) => r.id === roleId)
  const managers = managersFor(role, people)

  const titles = ['1. Choose their role', '2. Who do they report to?', '3. Their name and email']

  return (
    <Dialog
      open
      onClose={onClose}
      title={result ? `${result.person.full_name} is added` : `Add a person to ${company.name}`}
      description={result ? undefined : titles[step]}
      className="max-w-2xl"
      footer={
        result ? (
          <Button onClick={onClose}>Done</Button>
        ) : (
          <>
            <Button variant="ghost" onClick={step === 0 ? onClose : () => setStep(step - 1)}>
              {step === 0 ? (
                'Cancel'
              ) : (
                <>
                  <ArrowLeft aria-hidden="true" /> Back
                </>
              )}
            </Button>
            {step < 2 ? (
              <Button disabled={!roleId} onClick={() => setStep(step + 1)}>
                Next <ArrowRight aria-hidden="true" />
              </Button>
            ) : (
              <Button
                disabled={!name.trim() || !/.+@.+\..+/.test(email) || add.isPending}
                onClick={() =>
                  roleId &&
                  add.mutate(
                    {
                      full_name: name.trim(),
                      email: email.trim(),
                      role_id: roleId,
                      manager_id: managerId || null,
                    },
                    { onSuccess: setResult },
                  )
                }
              >
                {add.isPending ? 'Adding…' : 'Add person'}
              </Button>
            )}
          </>
        )
      }
    >
      {result ? (
        <SignInHandover
          name={result.person.full_name}
          email={result.person.email}
          signIn={result.sign_in}
        />
      ) : step === 0 ? (
        <div className="grid gap-2 sm:grid-cols-2">
          {sortedRoles(roles).map((r) => {
            const look = roleLook(r.name)
            return (
              <button
                key={r.id}
                type="button"
                onClick={() => {
                  setRoleId(r.id)
                  setManagerId('')
                }}
                className={cn(
                  'rounded-xl border p-3 text-left transition-colors',
                  roleId === r.id
                    ? 'border-primary bg-primary/5 ring-primary/40 ring-2'
                    : 'hover:bg-muted/40',
                )}
              >
                <RoleChip name={r.name} />
                <p className="mt-1.5 text-sm">{look.summary}</p>
                <ul className="text-muted-foreground mt-1 list-disc pl-4 text-xs">
                  {look.can.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </button>
            )
          })}
        </div>
      ) : step === 1 ? (
        <div className="space-y-2">
          <p className="text-sm">
            A <RoleChip name={role?.name ?? ''} /> usually reports to someone above them. Their
            manager will see their leads.
          </p>
          <Select
            aria-label="Reports to"
            value={managerId}
            onChange={(e) => setManagerId(e.target.value)}
          >
            <option value="">Nobody — top of the tree</option>
            {managers.map((m) => (
              <option key={m.membership_id} value={m.membership_id}>
                {m.full_name} ({m.role_name})
              </option>
            ))}
          </Select>
          {managers.length === 0 ? (
            <p className="text-muted-foreground text-xs">Nobody above this role yet.</p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-3">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Full name</span>
            <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Email (their login)</span>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <p className="text-muted-foreground text-xs">
            You’ll get a link to send them; they choose their own password. Nobody else ever sees
            it.
          </p>
          {add.error ? (
            <p role="alert" className="text-destructive text-sm">
              {errorText(add.error)}
            </p>
          ) : null}
        </div>
      )}
    </Dialog>
  )
}

function EditPersonDialog({
  company,
  person,
  roles,
  people,
  onClose,
}: {
  readonly company: Company
  readonly person: Person
  readonly roles: readonly Role[]
  readonly people: readonly Person[]
  readonly onClose: () => void
}) {
  const update = useUpdatePerson(company.id)
  const [roleId, setRoleId] = useState(person.role_id)
  const [managerId, setManagerId] = useState(person.manager_id ?? '')
  const [active, setActive] = useState(person.is_active)
  const [reassignTo, setReassignTo] = useState('')
  const role = roles.find((r) => r.id === roleId)
  const managers = managersFor(role, people, person.membership_id)
  const needsReassign =
    update.error instanceof ApiError && update.error.code === 'reassignment_required'

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Edit ${person.full_name}`}
      description={person.email}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={update.isPending}
            onClick={() =>
              update.mutate(
                {
                  membershipId: person.membership_id,
                  body: {
                    ...(roleId !== person.role_id ? { role_id: roleId } : {}),
                    ...(managerId !== (person.manager_id ?? '')
                      ? managerId
                        ? { manager_id: managerId }
                        : { clear_manager: true }
                      : {}),
                    ...(active !== person.is_active ? { is_active: active } : {}),
                    ...(reassignTo ? { reassign_to: reassignTo } : {}),
                  },
                },
                { onSuccess: onClose },
              )
            }
          >
            {update.isPending ? 'Saving…' : 'Save'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <label className="block space-y-1.5">
          <span className="text-sm font-medium">Role</span>
          <Select value={roleId} onChange={(e) => setRoleId(e.target.value)}>
            {sortedRoles(roles).map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </Select>
          <span className="text-muted-foreground block text-xs">
            {roleLook(role?.name ?? '').summary}
          </span>
        </label>
        <label className="block space-y-1.5">
          <span className="text-sm font-medium">Reports to</span>
          <Select value={managerId} onChange={(e) => setManagerId(e.target.value)}>
            <option value="">Nobody — top of the tree</option>
            {managers.map((m) => (
              <option key={m.membership_id} value={m.membership_id}>
                {m.full_name} ({m.role_name})
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-3">
          <Switch aria-label="Can sign in" checked={active} onCheckedChange={setActive} />
          <span className="text-sm">
            {active
              ? 'Can sign in and holds a seat'
              : 'Switched off — cannot sign in, frees a seat'}
          </span>
        </label>
        {needsReassign ? (
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">They still hold open leads. Hand them to:</span>
            <Select value={reassignTo} onChange={(e) => setReassignTo(e.target.value)}>
              <option value="">Choose…</option>
              {people
                .filter((p) => p.is_active && p.membership_id !== person.membership_id)
                .map((p) => (
                  <option key={p.membership_id} value={p.membership_id}>
                    {p.full_name} ({p.role_name})
                  </option>
                ))}
            </Select>
          </label>
        ) : null}
        {update.error ? (
          <p role="alert" className="text-destructive text-sm">
            {errorText(update.error)}
          </p>
        ) : null}
      </div>
    </Dialog>
  )
}
