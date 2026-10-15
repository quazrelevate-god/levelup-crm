import * as React from 'react'

import { cn } from '@/lib/utils'
import { fieldClasses } from '@/components/ui/input'

/**
 * A native `<select>`, styled to match the rest of the kit.
 *
 * Deliberately not Radix: M1 needs a plain picker for templates, managers and
 * availability. The searchable, virtualised combobox the lead filters want
 * arrives with M6, when there is something to search.
 */
function Select({ className, ...props }: React.ComponentProps<'select'>) {
  return (
    <select
      data-slot="select"
      className={cn(fieldClasses, 'flex h-9.5 py-1 pr-9', className)}
      {...props}
    />
  )
}

export { Select }
