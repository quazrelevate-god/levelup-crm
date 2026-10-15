import { Star } from 'lucide-react'

import { cn } from '@/lib/utils'

const VALUES = [1, 2, 3, 4, 5] as const

export interface StarRatingProps {
  readonly value: number | null
  /** Omit to render read-only — which is what the list needs. */
  readonly onChange?: (next: number | null) => void
  readonly disabled?: boolean
  readonly size?: 'sm' | 'md'
  readonly label?: string
}

/**
 * Five stars instead of a 1–5 dropdown.
 *
 * A rating is an ordered scale, and a select renders it as five unrelated
 * strings — you cannot see at a glance that 4 is more than 2, which is the
 * whole point of the field.
 *
 * Built as a radio group rather than five buttons, because that is what it is:
 * one choice out of five, exclusive. Screen readers then announce "Rating,
 * 4 of 5" rather than five separate pressable things. Each star keeps its own
 * accessible name, so the control is reachable without sight or a mouse, which
 * a row of glyphs otherwise is not.
 *
 * Clicking the current value clears it. The field is nullable and without this
 * there is no way back to "no rating" once one is set.
 */
export function StarRating({
  value,
  onChange,
  disabled = false,
  size = 'md',
  label = 'Rating',
}: StarRatingProps) {
  const readOnly = onChange === undefined
  const starSize = size === 'sm' ? 'size-3.5' : 'size-5'

  if (readOnly) {
    return (
      <span
        className="inline-flex items-center gap-0.5"
        // One label for the whole thing: five repeated star names in a table
        // cell is noise, and the number is what the reader actually wants.
        role="img"
        aria-label={value === null ? 'No rating' : `${value} out of 5`}
      >
        {VALUES.map((star) => (
          <Star
            key={star}
            aria-hidden="true"
            className={cn(
              starSize,
              value !== null && star <= value
                ? 'fill-foreground text-foreground'
                : 'text-muted-foreground/35',
            )}
          />
        ))}
      </span>
    )
  }

  return (
    <div role="radiogroup" aria-label={label} className="flex items-center gap-1">
      {VALUES.map((star) => {
        const selected = value !== null && star <= value
        return (
          <button
            key={star}
            type="button"
            role="radio"
            aria-checked={value === star}
            aria-label={`${star} out of 5`}
            disabled={disabled}
            onClick={() => onChange(value === star ? null : star)}
            className={cn(
              'rounded-md p-0.5 transition-transform duration-100 outline-none',
              'focus-visible:ring-ring/45 focus-visible:ring-[3px]',
              'disabled:cursor-not-allowed disabled:opacity-50',
              !disabled && 'hover:scale-110',
            )}
          >
            <Star
              aria-hidden="true"
              className={cn(
                starSize,
                selected ? 'fill-foreground text-foreground' : 'text-muted-foreground/40',
              )}
            />
          </button>
        )
      })}
      <span className="text-muted-foreground ml-1.5 text-xs tabular-nums">
        {value === null ? 'None' : `${value}/5`}
      </span>
    </div>
  )
}
