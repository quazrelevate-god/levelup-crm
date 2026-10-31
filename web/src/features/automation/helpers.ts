/** Small shared helpers for the automation screens. */

import type { Stage } from '@/api/types'
import type { useStages } from '@/features/pipeline/api'

export function allStages(pipeline: ReturnType<typeof useStages>['data']): Stage[] {
  if (!pipeline) return []
  return [pipeline.initial, ...pipeline.active, pipeline.won, pipeline.lost].filter(
    (stage): stage is Stage => stage !== null,
  )
}

export function amountText(data: Record<string, unknown>): string {
  const amount = data.amount
  if (amount === undefined || amount === null || amount === '') return ''
  const currency =
    typeof data.currency === 'string' && data.currency !== 'INR' ? data.currency : '₹'
  if (typeof amount !== 'string' && typeof amount !== 'number') return ''
  return `${currency}${amount}`
}
