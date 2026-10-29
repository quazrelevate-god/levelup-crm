import * as React from 'react'

import { cn } from '@/lib/utils'
import { fieldClasses } from '@/components/ui/input'

/**
 * A native `<select>`, styled to match the rest of the kit.
 *
 * Deliberately not Radix: M1 needs a plain picker for templates, managers and
 * availability. The searchable, virtualised combobox the lead filters want
 * arrives with M6, when there is something to search.
 *
 * The browser's own arrow ignores the rounded corners and sits on the edge,
 * so it is switched off (`appearance-none`) and a chevron is drawn as a
 * background image, inset from the right. Being part of the select itself, it
 * fits any width a caller sets without a wrapper element.
 */
const CHEVRON =
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")"

function Select({ className, style, ...props }: React.ComponentProps<'select'>) {
  return (
    <select
      data-slot="select"
      className={cn(
        fieldClasses,
        'flex h-9.5 cursor-pointer appearance-none bg-[length:1rem] bg-[position:right_0.75rem_center] bg-no-repeat py-1 pr-9',
        className,
      )}
      style={{ backgroundImage: CHEVRON, ...style }}
      {...props}
    />
  )
}

export { Select }
