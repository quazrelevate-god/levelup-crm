/**
 * How each level of the hierarchy looks and what it means, so the console can
 * guide the operator: Operator → Company → Root → Admin → Manager → Caller.
 *
 * Roles are a company's own data and may be renamed or added; the five a
 * company starts with get their own colour and explanation, anything else is
 * shown as a custom role.
 */

export interface RoleLook {
  readonly rank: number
  readonly chip: string
  readonly dot: string
  readonly summary: string
  readonly can: readonly string[]
}

const LOOKS: Record<string, RoleLook> = {
  Root: {
    rank: 0,
    chip: 'bg-violet-600 text-white',
    dot: 'bg-violet-600',
    summary: "The company's owner. Full control, and this role can never be locked out.",
    can: ['Everything in their CRM', 'Add and remove anyone', 'Change every setting'],
  },
  Admin: {
    rank: 1,
    chip: 'bg-indigo-600 text-white',
    dot: 'bg-indigo-600',
    summary: 'Runs the CRM day to day with full control, like Root.',
    can: ['Everything in their CRM', 'Add people and set roles', 'Change settings'],
  },
  Manager: {
    rank: 2,
    chip: 'bg-sky-600 text-white',
    dot: 'bg-sky-600',
    summary: 'Leads a team: sees their own leads and everyone who reports to them.',
    can: ["Their team's leads", 'Reassign leads in the team', 'Team reports'],
  },
  Caller: {
    rank: 3,
    chip: 'bg-emerald-600 text-white',
    dot: 'bg-emerald-600',
    summary: 'Works their own leads: calls, notes, follow-ups.',
    can: ['Only their own leads', 'Log calls and notes', 'Tasks and follow-ups'],
  },
  Marketing: {
    rank: 3,
    chip: 'bg-amber-500 text-white',
    dot: 'bg-amber-500',
    summary: 'Brings leads in: adds and imports leads, sees campaign results.',
    can: ['Add and import leads', 'Campaigns', 'Lead source reports'],
  },
}

const CUSTOM: RoleLook = {
  rank: 4,
  chip: 'bg-slate-500 text-white',
  dot: 'bg-slate-500',
  summary: 'A role this company made itself.',
  can: ['Whatever the company set for this role'],
}

export function roleLook(name: string): RoleLook {
  return LOOKS[name] ?? CUSTOM
}

/** The levels the console draws, top to bottom. */
export const LEVELS = ['Root', 'Admin', 'Manager', 'Caller'] as const

export function money(value: string | null | undefined, currency: string): string {
  if (value === null || value === undefined) return '—'
  const amount = Number(value)
  const symbol = currency === 'INR' ? '₹' : `${currency} `
  return `${symbol}${amount.toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`
}
