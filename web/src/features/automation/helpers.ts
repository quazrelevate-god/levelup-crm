/** Small shared helpers for the automation screens. */

import { ApiError } from '@/api/client'
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

/** A failed request, in words. */
export function errorMessage(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'That did not work. Please try again.'
}

/** A setting's text, or `fallback` when it is missing or not text. */
export function settingText(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}
