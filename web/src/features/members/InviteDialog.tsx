import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { MemberDetail } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useInviteMember } from '@/features/members/api'

/**
 * Invite someone into the workspace.
 *
 * The template list comes from the workspace's own templates — never a
 * hardcoded role list. A customer who renamed "Caller" to something else, or
 * added three of their own, must see exactly what they configured.
 */
interface Props {
  readonly workspaceId: string
  readonly open: boolean
  readonly templates: readonly { id: string; name: string }[]
  readonly members: readonly MemberDetail[]
  readonly onClose: () => void
}

const MESSAGES: Record<string, string> = {
  member_exists: 'That person is already a member of this workspace.',
  seat_limit_reached:
    'All licensed seats are in use. Free one up, or ask your account manager for more seats.',
  not_found: 'That permission template no longer exists. Reload and try again.',
}

export function InviteDialog({ workspaceId, open, templates, members, onClose }: Props) {
  const invite = useInviteMember(workspaceId)

  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [managerId, setManagerId] = useState('')
  const [grantLicense, setGrantLicense] = useState(true)
  const [error, setError] = useState<string | null>(null)

  function reset() {
    setEmail('')
    setFullName('')
    setTemplateId('')
    setManagerId('')
    setGrantLicense(true)
    setError(null)
    onClose()
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      await invite.mutateAsync({
        email,
        full_name: fullName,
        template_id: templateId,
        manager_id: managerId || null,
        grant_license: grantLicense,
      })
      reset()
    } catch (cause) {
      const code = cause instanceof ApiError ? cause.code : 'unknown_error'
      setError(MESSAGES[code] ?? 'Could not invite this person.')
    }
  }

  return (
    <Dialog
      open={open}
      onClose={reset}
      title="Invite a member"
      description="They receive a password reset to set their own credentials."
      footer={
        <>
          <Button variant="ghost" onClick={reset}>
            Cancel
          </Button>
          <Button
            type="submit"
            form="invite-member"
            disabled={invite.isPending || !email || !fullName || !templateId}
          >
            {invite.isPending ? 'Inviting…' : 'Invite'}
          </Button>
        </>
      }
    >
      <form
        id="invite-member"
        className="flex flex-col gap-4"
        onSubmit={(event) => void submit(event)}
        noValidate
      >
        <div className="flex flex-col gap-2">
          <Label htmlFor="invite-email">Email</Label>
          <Input
            id="invite-email"
            type="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="invite-name">Full name</Label>
          <Input
            id="invite-name"
            required
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
          />
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="invite-template">Permission template</Label>
          <Select
            id="invite-template"
            required
            value={templateId}
            onChange={(event) => setTemplateId(event.target.value)}
          >
            <option value="">Select a template…</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="invite-manager">Reports to (optional)</Label>
          <Select
            id="invite-manager"
            value={managerId}
            onChange={(event) => setManagerId(event.target.value)}
          >
            <option value="">Nobody</option>
            {members
              .filter((member) => member.is_active)
              .map((member) => (
                <option key={member.id} value={member.id}>
                  {member.user.full_name}
                </option>
              ))}
          </Select>
        </div>

        <Label className="gap-2">
          <input
            type="checkbox"
            checked={grantLicense}
            onChange={(event) => setGrantLicense(event.target.checked)}
          />
          Assign a licensed seat now
        </Label>

        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        ) : null}
      </form>
    </Dialog>
  )
}
