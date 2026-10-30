/**
 * Agent variables — the names to use in the voice agent's script (docs/15).
 *
 * Every lead field you can see reaches the agent on every call, written
 * `{key}` in Bolna's prompt, and empty when the lead has no value. The CRM also
 * sends its own `crm_*` variables: what earlier calls said, how many there
 * were, why this call is being made. This page lists both, from the server's
 * own catalogue, so the script and the CRM cannot drift apart.
 */

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { PageHeader } from '@/components/ui/page-header'
import { useAuth } from '@/features/auth/context'
import { useVoiceVariables, type VoiceVariable } from '@/features/voice/agentContext'

function CopyName({ name }: { readonly name: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      size="sm"
      variant="ghost"
      aria-label={`Copy {${name}}`}
      onClick={() => {
        void navigator.clipboard
          .writeText(`{${name}}`)
          .then(() => {
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          })
          .catch(() => setCopied(false))
      }}
    >
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
    </Button>
  )
}

function VariableTable({ rows }: { readonly rows: readonly VoiceVariable[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-muted-foreground border-b text-left text-xs">
            <th className="py-2 pr-3 font-medium">In the script</th>
            <th className="py-2 pr-3 font-medium">What it holds</th>
            <th className="py-2 font-medium">Sent</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.name} className="border-border/50 border-b last:border-b-0">
              <td className="py-2 pr-3 align-top whitespace-nowrap">
                <span className="font-mono text-xs">{`{${row.name}}`}</span>
                <CopyName name={row.name} />
              </td>
              <td className="py-2 pr-3 align-top">
                <p className="font-medium">{row.label}</p>
                {row.description ? (
                  <p className="text-muted-foreground text-xs">{row.description}</p>
                ) : null}
              </td>
              <td className="py-2 align-top">
                <Badge variant={row.always_sent ? 'outline' : 'secondary'}>
                  {row.always_sent ? 'Every call' : 'When known'}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function VoiceVariablesPage() {
  const { activeWorkspaceId } = useAuth()
  const variables = useVoiceVariables(activeWorkspaceId as string)
  const code = variables.error instanceof ApiError ? variables.error.code : null
  const own = variables.data?.filter((row) => row.kind === 'field') ?? []
  const facts = variables.data?.filter((row) => row.kind === 'fact') ?? []
  const reserved = variables.data?.filter((row) => row.kind === 'crm') ?? []

  return (
    <div className="space-y-5">
      <PageHeader
        title="Agent variables"
        description="The names to use in the voice agent's script. Paste them into the Bolna prompt with the curly brackets."
      />

      {variables.isPending ? (
        <p className="text-muted-foreground text-sm">Loading variables…</p>
      ) : code === 'insufficient_permissions' ? (
        <p className="text-muted-foreground text-sm">
          Your permission template does not include call history.
        </p>
      ) : variables.isError ? (
        <p className="text-muted-foreground text-sm">The variables could not be loaded.</p>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Your lead fields</CardTitle>
              <CardDescription>
                Sent on every call. When a lead has no value the variable arrives empty, so tell the
                agent never to guess a blank one. Add a field in Settings → Fields and it appears
                here.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <VariableTable rows={own} />
            </CardContent>
          </Card>
          {facts.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Call facts</CardTitle>
                <CardDescription>
                  From Settings → Call facts: the first set a lead matches decides the value. Sent
                  on every call, empty when the lead&apos;s set does not have it.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <VariableTable rows={facts} />
              </CardContent>
            </Card>
          ) : null}
          <Card>
            <CardHeader>
              <CardTitle>From the CRM</CardTitle>
              <CardDescription>
                What the CRM knows about the relationship. &ldquo;When known&rdquo; variables are
                absent on a first call, so the script should treat them as optional.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <VariableTable rows={reserved} />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
