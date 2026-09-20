/**
 * How a call's numbers read to a person.
 *
 * Shared by the lead panel's AI Call card, the call list, the call page and the
 * timeline entry, so one call never reads as `1m 57s` in one place and `117s`
 * in another.
 */

/** `117` → `1m 57s`, `45` → `45s`. */
export function formatCallDuration(value: unknown): string {
  const total = Math.max(0, Math.round(Number(value) || 0))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`
}

/**
 * The vendor's status string, for people: `no-answer` → `No answer`.
 *
 * Deliberately a presentation transform rather than a lookup table: Bolna is
 * entitled to add statuses, and an unknown one should still render as words
 * rather than disappear.
 */
export function formatCallStatus(value: unknown): string {
  // Only a string is a status. Anything else (a number, an object a vendor
  // started sending) is unknown rather than stringified into noise.
  const text = typeof value === 'string' ? value.trim() : ''
  if (!text) return 'Unknown'
  const words = text.replace(/[-_]/g, ' ').toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** `20 Sep 2026, 12:57` in the viewer's locale, or an em dash. */
export function formatCallMoment(value: string | null | undefined): string {
  if (!value) return '—'
  const when = new Date(value)
  if (Number.isNaN(when.getTime())) return '—'
  return when.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
