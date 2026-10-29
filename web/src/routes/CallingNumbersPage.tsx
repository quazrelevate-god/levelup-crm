/**
 * Calling numbers (docs/16) — the numbers AI calls go out from.
 *
 * Calls rotate through the list, one number per call, so no single number
 * makes every call. When a number starts getting flagged as spam, replace it
 * here: the next call uses the new list. Empty means Bolna's default number.
 */

import { useEffect, useState } from 'react'
import { Phone } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { PageHeader } from '@/components/ui/page-header'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/features/auth/context'
import { useCallingNumbers, useSetCallingNumbers } from '@/features/voice/callingNumbers'

export function CallingNumbersPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const numbers = useCallingNumbers(workspaceId)
  const save = useSetCallingNumbers(workspaceId)
  const [draft, setDraft] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (numbers.data) setDraft(numbers.data.numbers.join('\n'))
  }, [numbers.data])

  const error =
    save.error instanceof ApiError ? save.error.message : save.error ? 'Could not save.' : null

  return (
    <div className="space-y-5">
      <PageHeader
        title="Calling numbers"
        description="The numbers AI calls go out from. Calls take turns across the list, one number per call."
      />

      {numbers.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : numbers.isError ? (
        <p className="text-muted-foreground text-sm">The calling numbers could not be loaded.</p>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Phone aria-hidden="true" className="size-4" />
              {numbers.data.numbers.length === 0
                ? "Bolna's default number"
                : `${numbers.data.numbers.length} number${numbers.data.numbers.length === 1 ? '' : 's'} in rotation`}
            </CardTitle>
            <CardDescription>
              {numbers.data.editable
                ? 'One number per line. When a number starts being flagged as spam, replace it here — the next call uses the new list.'
                : 'Managed by your account manager. Ask them to add or replace a number.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {numbers.data.editable ? (
              <form
                className="space-y-3"
                onSubmit={(event) => {
                  event.preventDefault()
                  setSaved(false)
                  save.mutate(
                    draft.split('\n').map((line) => line.trim()),
                    { onSuccess: () => setSaved(true) },
                  )
                }}
              >
                <div className="space-y-1.5">
                  <Label htmlFor="calling-numbers">Numbers</Label>
                  <Textarea
                    id="calling-numbers"
                    rows={5}
                    placeholder={'+91 80000 00000\n+91 80000 00001'}
                    value={draft}
                    onChange={(event) => {
                      setDraft(event.target.value)
                      setSaved(false)
                    }}
                    className="font-mono"
                  />
                </div>
                {error ? (
                  <p role="alert" className="text-destructive text-sm">
                    {error}
                  </p>
                ) : null}
                <div className="flex items-center gap-3">
                  <Button type="submit" size="sm" disabled={save.isPending}>
                    {save.isPending ? 'Saving…' : 'Save numbers'}
                  </Button>
                  {saved ? (
                    <span role="status" className="text-muted-foreground text-sm">
                      Saved. The next call uses this list.
                    </span>
                  ) : null}
                </div>
              </form>
            ) : numbers.data.numbers.length > 0 ? (
              <ul className="flex flex-wrap gap-2">
                {numbers.data.numbers.map((number) => (
                  <li key={number}>
                    <Badge variant="outline" className="font-mono">
                      {number}
                    </Badge>
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="text-muted-foreground text-xs">
              Numbers are bought in Bolna (Phone numbers). Each call's page shows the number it went
              out from, under “Called from”.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
