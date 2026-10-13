import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A native checkbox, styled to match the kit.
 *
 * Native rather than Radix because the permission matrix renders one per field
 * per grant — four columns over a workspace's whole schema — and a controlled
 * Radix primitive at that count is measurably slower to type into.
 *
 * Its tick colour comes from `accent-color` in the base layer, so it follows
 * the brand instead of the browser's own blue.
 */
function Checkbox({ className, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type="checkbox"
      data-slot="checkbox"
      className={cn(
        'border-input size-4 shrink-0 cursor-pointer rounded-[5px] border transition-[box-shadow] outline-none',
        'focus-visible:ring-ring/40 focus-visible:border-ring focus-visible:ring-[3px]',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    />
  )
}

export { Checkbox }
