/** What was done from the console, newest first (docs/19). */

import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

import { useCompanies, useHistory } from './api'

const LABELS: Record<string, string> = {
  'company.created': 'Company created',
  'company.updated': 'Company changed',
  'company.suspended': 'Suspended',
  'person.added': 'Person added',
  'person.updated': 'Person changed',
  'person.sign_in_link': 'Sign-in link',
  'wallet.topup': 'Money added',
  'wallet.discount': 'Free credit',
  'wallet.adjustment': 'Correction',
  'wallet.rate': 'Price changed',
  'support.started': 'Went inside',
  'support.ended': 'Left',
  'number.fulfilled': 'Number given',
  'number.cancelled': 'Number cancelled',
  'operator.sign_in_link': 'Operator link',
}

export function HistoryList({ workspaceId }: { readonly workspaceId: string | null }) {
  const [offset, setOffset] = useState(0)
  const history = useHistory(workspaceId, offset)
  const companies = useCompanies()
  const names = new Map((companies.data ?? []).map((c) => [c.id, c.name]))

  if (history.isPending) return <p className="text-muted-foreground text-sm">Loading…</p>
  if (history.isError)
    return <p className="text-muted-foreground text-sm">History could not be loaded.</p>
  if (history.data.items.length === 0) {
    return <p className="text-muted-foreground text-sm">Nothing has been done here yet.</p>
  }
  return (
    <div className="space-y-3">
      <ol className="space-y-2">
        {history.data.items.map((h) => (
          <li key={h.id} className="flex flex-wrap items-start gap-3 rounded-lg border px-3 py-2">
            <Badge
              variant={
                h.action.startsWith('wallet')
                  ? 'success'
                  : h.action.includes('suspend')
                    ? 'destructive'
                    : 'outline'
              }
            >
              {LABELS[h.action] ?? h.action}
            </Badge>
            <span className="min-w-0 flex-1 text-sm">
              {h.summary}
              {!workspaceId && h.workspace_id && names.get(h.workspace_id) ? (
                <span className="text-muted-foreground"> · {names.get(h.workspace_id)}</span>
              ) : null}
            </span>
            <span className="text-muted-foreground text-xs whitespace-nowrap">
              {new Date(h.created_at).toLocaleString()} · {h.actor_email}
            </span>
          </li>
        ))}
      </ol>
      {history.data.total > 20 ? (
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 20))}
          >
            Newer
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={offset + 20 >= history.data.total}
            onClick={() => setOffset(offset + 20)}
          >
            Older
          </Button>
        </div>
      ) : null}
    </div>
  )
}
