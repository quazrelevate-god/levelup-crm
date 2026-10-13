import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { AvailabilityStatus, MemberDetail, PermissionTemplateSummary } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { usePermissions } from '@/features/auth/usePermissions'
import {
  useMembers,
  useReactivateMember,
  useSeatUsage,
  useSetAvailability,
  useToggleLicense,
} from '@/features/members/api'
import { BulkUploadDialog } from '@/features/members/BulkUploadDialog'
import { DeactivateDialog } from '@/features/members/DeactivateDialog'
import { InviteDialog } from '@/features/members/InviteDialog'

const AVAILABILITY_LABELS: Record<AvailabilityStatus, string> = {
  WORKING: 'Working',
  ON_LEAVE: 'On leave',
  INACTIVE: 'Inactive',
}

function availabilityVariant(status: AvailabilityStatus) {
  if (status === 'WORKING') return 'success' as const
  if (status === 'ON_LEAVE') return 'secondary' as const
  return 'destructive' as const
}

/**
 * Copy for the codes an admin can act on.
 *
 * The API's own messages are accurate but describe the refusal; these describe
 * the fix. Anything not listed falls back to the server's message, which is
 * always safe to show.
 */
const ACTION_MESSAGES: Record<string, string> = {
  seat_limit_reached:
    'All licensed seats are in use. Revoke a seat, or raise the seat limit in workspace settings.',
  cannot_deactivate_self: 'You cannot deactivate your own membership. Ask another administrator.',
  use_deactivate_endpoint: 'Use Deactivate so any open leads are reassigned first.',
  not_own_membership: 'You can only change your own availability.',
  insufficient_permissions: 'Your permission template does not allow this.',
}

function actionMessage(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  return ACTION_MESSAGES[cause.code] ?? cause.message
}

export function MembersPage() {
  const { activeWorkspaceId, activeMembership } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const { isAdmin } = usePermissions()

  const members = useMembers(workspaceId)
  const seats = useSeatUsage(workspaceId)
  const templates = useQuery({
    queryKey: ['permission-templates', workspaceId],
    queryFn: () =>
      api.get<PermissionTemplateSummary[]>(
        `/workspaces/${workspaceId}/settings/permission-templates`,
      ),
  })

  const setAvailability = useSetAvailability(workspaceId)
  const toggleLicense = useToggleLicense(workspaceId)
  const reactivate = useReactivateMember(workspaceId)

  const [inviteOpen, setInviteOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [deactivating, setDeactivating] = useState<MemberDetail | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const rows = members.data?.items ?? []

  async function run(work: () => Promise<unknown>) {
    setActionError(null)
    try {
      await work()
    } catch (cause) {
      setActionError(actionMessage(cause))
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Team</h1>
          {seats.data ? (
            <p className="text-muted-foreground text-sm">
              {seats.data.seats_used} of {seats.data.seat_limit} licensed seats in use
            </p>
          ) : null}
        </div>

        {isAdmin ? (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setUploadOpen(true)}>
              Upload
            </Button>
            <Button onClick={() => setInviteOpen(true)}>Invite member</Button>
          </div>
        ) : null}
      </header>

      {actionError ? (
        <p role="alert" className="text-destructive text-sm">
          {actionError}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>
            {members.isLoading ? 'Loading…' : `${members.data?.total ?? 0} members`}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground border-b text-left">
                  <th className="px-2 py-2 font-medium">Name</th>
                  <th className="px-2 py-2 font-medium">Email</th>
                  <th className="px-2 py-2 font-medium">Template</th>
                  <th className="px-2 py-2 font-medium">Availability</th>
                  <th className="px-2 py-2 font-medium">Seat</th>
                  <th className="px-2 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {rows.map((member) => {
                  const isSelf = member.id === activeMembership?.id
                  const canEditAvailability = isAdmin || isSelf

                  return (
                    <tr key={member.id} className="border-b last:border-0">
                      <td className="px-2 py-2">
                        {member.user.full_name}
                        {isSelf ? (
                          <span className="text-muted-foreground ml-2 text-xs">you</span>
                        ) : null}
                      </td>
                      <td className="text-muted-foreground px-2 py-2">{member.user.email}</td>
                      <td className="px-2 py-2">{member.template_name}</td>

                      <td className="px-2 py-2">
                        {member.is_active && canEditAvailability ? (
                          <Select
                            aria-label={`Availability for ${member.user.full_name}`}
                            className="h-8 w-32"
                            value={member.availability}
                            disabled={setAvailability.isPending}
                            onChange={(event) =>
                              void run(() =>
                                setAvailability.mutateAsync({
                                  membershipId: member.id,
                                  status: event.target.value as AvailabilityStatus,
                                  note: null,
                                }),
                              )
                            }
                          >
                            {/* INACTIVE is deliberately absent: that is
                                deactivation, which must reassign leads first. */}
                            <option value="WORKING">Working</option>
                            <option value="ON_LEAVE">On leave</option>
                          </Select>
                        ) : (
                          <Badge variant={availabilityVariant(member.availability)}>
                            {AVAILABILITY_LABELS[member.availability]}
                          </Badge>
                        )}
                      </td>

                      <td className="px-2 py-2">
                        {member.has_license ? (
                          <Badge variant="success">Licensed</Badge>
                        ) : (
                          <Badge variant="outline">No seat</Badge>
                        )}
                      </td>

                      <td className="px-2 py-2">
                        {isAdmin && !isSelf ? (
                          <div className="flex justify-end gap-2">
                            {member.is_active ? (
                              <>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  disabled={toggleLicense.isPending}
                                  onClick={() =>
                                    void run(() =>
                                      toggleLicense.mutateAsync({
                                        membershipId: member.id,
                                        grant: !member.has_license,
                                      }),
                                    )
                                  }
                                >
                                  {member.has_license ? 'Revoke seat' : 'Assign seat'}
                                </Button>
                                <Button
                                  variant="destructive"
                                  size="sm"
                                  onClick={() => setDeactivating(member)}
                                >
                                  Deactivate
                                </Button>
                              </>
                            ) : (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={reactivate.isPending}
                                onClick={() => void run(() => reactivate.mutateAsync(member.id))}
                              >
                                Reactivate
                              </Button>
                            )}
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <InviteDialog
        workspaceId={workspaceId}
        open={inviteOpen}
        templates={templates.data ?? []}
        members={rows}
        onClose={() => setInviteOpen(false)}
      />
      <BulkUploadDialog
        workspaceId={workspaceId}
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
      />
      <DeactivateDialog
        workspaceId={workspaceId}
        member={deactivating}
        candidates={rows}
        onClose={() => setDeactivating(null)}
      />
    </div>
  )
}
