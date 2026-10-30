/**
 * The navigation model, in one place because two screens render it: the app
 * sidebar and the settings hub.
 *
 * The split is the point. The old build put eighteen destinations in a single
 * flat row, so "Leads" — where a salesperson lives all day — sat as a peer of
 * "System", which only an operator ever opens. Everything that configures the
 * workspace or inspects the deployment now lives behind one door.
 */

import {
  Activity,
  BookOpenText,
  Braces,
  BarChart3,
  CalendarClock,
  FileSpreadsheet,
  GitBranch,
  History,
  LayoutDashboard,
  type LucideIcon,
  MessageSquareText,
  Phone,
  Plug,
  ListChecks,
  ShieldCheck,
  SlidersHorizontal,
  Table2,
  Upload,
  UserCog,
  Users,
  Wallet,
  Waypoints,
} from 'lucide-react'

export interface NavLink {
  readonly to: string
  readonly label: string
  readonly icon: LucideIcon
  readonly description?: string
}

export interface NavGroup {
  readonly heading: string
  readonly links: readonly NavLink[]
}

/** Daily work. What a salesperson or a caller opens without thinking. */
export const PRIMARY_NAV: readonly NavLink[] = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/leads', label: 'Leads', icon: Users },
  { to: '/calls', label: 'Call details', icon: Phone },
  { to: '/tasks', label: 'Tasks', icon: ListChecks },
]

/** Still day-to-day, but a manager's day rather than a caller's. */
export const WORKSPACE_NAV: readonly NavLink[] = [
  { to: '/import', label: 'Import', icon: Upload },
  { to: '/templates', label: 'Templates', icon: MessageSquareText },
  { to: '/edit-report', label: 'Edit report', icon: History },
  { to: '/members', label: 'Team', icon: Users },
  { to: '/wallet', label: 'Wallet', icon: Wallet },
]

/**
 * Behind the Settings door. Configuration and deployment internals — the
 * screens a client should not meet by accident.
 */
export const SETTINGS_GROUPS: readonly NavGroup[] = [
  {
    heading: 'Data model',
    links: [
      {
        to: '/settings/fields',
        label: 'Fields',
        icon: Table2,
        description: 'The lead schema: what a lead is made of, and how each value is typed.',
      },
      {
        to: '/settings/pipeline',
        label: 'Pipeline',
        icon: GitBranch,
        description: 'Stages, lost reasons and call dispositions.',
      },
      {
        to: '/settings/custom-actions',
        label: 'Actions',
        icon: Waypoints,
        description: 'The activity types a member can log against a lead.',
      },
    ],
  },
  {
    heading: 'People and automation',
    links: [
      {
        to: '/settings/permissions',
        label: 'Permissions',
        icon: ShieldCheck,
        description: 'Named templates granting view, edit, import and export per field.',
      },
      {
        to: '/settings/assignment',
        label: 'Assignment',
        icon: UserCog,
        description: 'Rules that route an incoming lead to a member automatically.',
      },
      {
        to: '/settings/scheduled-reports',
        label: 'Schedules',
        icon: CalendarClock,
        description: 'Reports that send themselves on a cron.',
      },
      {
        to: '/settings/dashboards',
        label: 'Dashboards',
        icon: BarChart3,
        description: 'The widget layout every member lands on.',
      },
    ],
  },
  {
    heading: 'Platform',
    links: [
      {
        to: '/settings/integrations',
        label: 'Integrations',
        icon: Plug,
        description: 'API keys, webhooks, the delivery queue and the intake log.',
      },
      {
        to: '/settings/voice-extraction',
        label: 'Voice extraction',
        icon: SlidersHorizontal,
        description: 'How a Bolna call result maps onto fields, stages and actions.',
      },
      {
        to: '/settings/call-facts',
        label: 'Call facts',
        icon: BookOpenText,
        description:
          'Details the voice agent tells each lead — batch dates, mentors — chosen by rules.',
      },
      {
        to: '/settings/calling-numbers',
        label: 'Calling numbers',
        icon: Phone,
        description: 'The numbers AI calls go out from, taking turns across the list.',
      },
      {
        to: '/settings/voice-variables',
        label: 'Agent variables',
        icon: Braces,
        description: "The {names} to use in the voice agent's script, and what each holds.",
      },
      {
        to: '/settings/system',
        label: 'System',
        icon: Activity,
        description: 'Dependency health, build identity and queue depth.',
      },
    ],
  },
]

export const SETTINGS_LINKS: readonly NavLink[] = SETTINGS_GROUPS.flatMap((group) => group.links)

/** Longest-prefix match, so `/settings/fields` never lights up `/settings`. */
export function isActive(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(to + '/')
}

export const ALL_NAV_LINKS: readonly NavLink[] = [
  ...PRIMARY_NAV,
  ...WORKSPACE_NAV,
  ...SETTINGS_LINKS,
  { to: '/settings', label: 'Settings', icon: SlidersHorizontal },
]

export type { LucideIcon }
export { FileSpreadsheet }
