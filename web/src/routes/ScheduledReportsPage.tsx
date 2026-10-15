/**
 * Scheduled reports (M8).
 *
 * Two things this screen has to say out loud, because both are invisible and
 * both surprise people:
 *
 * **The cadence is in the workspace's timezone.** Not the browser's. A manager
 * in a different zone from their team would otherwise read "09:00" as their own
 * nine o'clock and file a bug when it arrives at a different time.
 *
 * **The report renders as whoever created it.** So what lands in a recipient's
 * inbox is bounded by that member's field permissions, not the recipient's and
 * not the reader's. A schedule whose creator has left cannot run at all, and
 * says so rather than failing quietly every morning.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import {
  useCreateScheduledReport,
  useDeleteScheduledReport,
  useRunScheduledReportNow,
  useScheduledReports,
} from '@/features/routing/api'

/** Cadences an operator actually wants, rather than a cron text box. */
const CADENCES: ReadonlyArray<{ cron: string; label: string }> = [
  { cron: '0 9 * * *', label: 'Every day at 09:00' },
  { cron: '0 9 * * 1-5', label: 'Every weekday at 09:00' },
  { cron: '0 9 * * 1', label: 'Mondays at 09:00' },
  { cron: '0 9 1 * *', label: 'First of the month at 09:00' },
  { cron: '0 * * * *', label: 'Every hour' },
]

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow scheduling reports.'
  }
  if (cause.code === 'orphaned_schedule') {
    return 'The member who created this schedule has left, so there are no field permissions to render it with.'
  }
  if (cause.code === 'unknown_report_type') return cause.message
  return cause.message
}

function describeCron(cron: string): string {
  return CADENCES.find((entry) => entry.cron === cron)?.label ?? cron
}

export function ScheduledReportsPage() {
  const { activeWorkspaceId, activeMembership } = useAuth()
  const workspaceId = activeWorkspaceId as string
  // The workspace's zone, never the browser's. This screen exists partly to
  // say which one it is.
  const timezone = activeMembership?.workspace.timezone ?? 'UTC'

  const reports = useScheduledReports(workspaceId)
  const remove = useDeleteScheduledReport(workspaceId)
  const runNow = useRunScheduledReportNow(workspaceId)

  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Scheduled reports</h1>
          <p className="text-muted-foreground text-sm">
            Sent on the workspace&rsquo;s clock &mdash; <strong>{timezone}</strong>.
          </p>
        </div>
        <Button onClick={() => setOpen(true)}>Schedule a report</Button>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p role="status" className="text-sm">
          {notice}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Schedules</CardTitle>
        </CardHeader>
        <CardContent>
          {reports.isError ? (
            <p role="alert" className="text-destructive py-8 text-center text-sm">
              {message(reports.error)}
            </p>
          ) : (reports.data ?? []).length === 0 && !reports.isLoading ? (
            <p className="text-muted-foreground py-8 text-center text-sm">Nothing scheduled.</p>
          ) : (
            <ul className="space-y-2">
              {(reports.data ?? []).map((report) => (
                <li
                  key={report.id}
                  data-testid="scheduled-report"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <div className="min-w-56 flex-1">
                    <span className="font-medium">{report.name}</span>
                    <span className="text-muted-foreground block text-xs">
                      {describeCron(report.cron)} · {report.recipients.length} recipient
                      {report.recipients.length === 1 ? '' : 's'} · {report.report_type}
                    </span>
                    {/* A broken schedule has to be visible here. Otherwise it
                        fails every morning and nobody finds out until somebody
                        asks where the report went. */}
                    {report.last_error ? (
                      <span className="text-destructive block text-xs">
                        Last run failed: {report.last_error}
                      </span>
                    ) : null}
                  </div>
                  {!report.is_active ? <Badge variant="outline">Inactive</Badge> : null}
                  {report.created_by === null ? (
                    <Badge variant="outline" className="border-destructive text-destructive">
                      No creator
                    </Badge>
                  ) : null}
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={runNow.isPending}
                    onClick={() => {
                      setError(null)
                      setNotice(null)
                      runNow.mutate(report.id, {
                        onError: (cause) => setError(message(cause)),
                        onSuccess: () => setNotice(`Sent ${report.name}.`),
                      })
                    }}
                  >
                    Send now
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setError(null)
                      remove.mutate(report.id, {
                        onError: (cause) => setError(message(cause)),
                      })
                    }}
                  >
                    Deactivate
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <NewScheduleDialog
        open={open}
        timezone={timezone}
        onClose={() => setOpen(false)}
        onError={setError}
      />
    </div>
  )
}

function NewScheduleDialog({
  open,
  timezone,
  onClose,
  onError,
}: {
  readonly open: boolean
  readonly timezone: string
  readonly onClose: () => void
  readonly onError: (message: string | null) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const create = useCreateScheduledReport(activeWorkspaceId as string)

  const [name, setName] = useState('')
  const [cron, setCron] = useState(CADENCES[0]?.cron ?? '0 9 * * *')
  const [recipients, setRecipients] = useState('')

  const addresses = recipients
    .split(/[,\s]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Schedule a report"
      description="It will render with your field permissions, not the recipients'."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || addresses.length === 0 || create.isPending}
            onClick={() => {
              onError(null)
              create.mutate(
                {
                  name: name.trim(),
                  report_type: 'leads',
                  cron,
                  recipients: addresses,
                },
                {
                  onError: (cause) => onError(message(cause)),
                  onSuccess: () => {
                    setName('')
                    setRecipients('')
                    onClose()
                  },
                },
              )
            }}
          >
            Schedule
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="schedule-name">Name</FieldLabel>
          <Input
            id="schedule-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Monday pipeline"
          />
        </div>

        <div className="space-y-1.5">
          <FieldLabel htmlFor="schedule-cron">Cadence</FieldLabel>
          <Select id="schedule-cron" value={cron} onChange={(event) => setCron(event.target.value)}>
            {CADENCES.map((entry) => (
              <option key={entry.cron} value={entry.cron}>
                {entry.label}
              </option>
            ))}
          </Select>
          <p className="text-muted-foreground text-xs">
            In {timezone} &mdash; the workspace&rsquo;s clock, not your browser&rsquo;s.
          </p>
        </div>

        <div className="space-y-1.5">
          <FieldLabel htmlFor="schedule-recipients">Recipients</FieldLabel>
          <Input
            id="schedule-recipients"
            value={recipients}
            onChange={(event) => setRecipients(event.target.value)}
            placeholder="ops@example.com, manager@example.com"
          />
          <p className="text-muted-foreground text-xs">
            {addresses.length === 0
              ? 'Comma or space separated.'
              : `${addresses.length} recipient${addresses.length === 1 ? '' : 's'}.`}
          </p>
        </div>
      </div>
    </Dialog>
  )
}
