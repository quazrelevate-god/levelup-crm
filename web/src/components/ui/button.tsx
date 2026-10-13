import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * Material's button, in this system's clothes.
 *
 * The state layer — a tint of the control's own foreground, faded in on hover
 * and press — is what makes one set of variants read correctly on glass, on a
 * card and on the page background alike. A fixed hover colour cannot, because
 * it has to assume a backdrop.
 *
 * Elevation is the only neumorphic touch, and only on `default`: a filled
 * button is the one control that genuinely sits above the page.
 */
const buttonVariants = cva(
  [
    'state-layer inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap',
    'rounded-full text-sm font-medium tracking-tight',
    'transition-[background-color,box-shadow,color,transform] duration-150 ease-[cubic-bezier(0.2,0,0.2,1)]',
    'outline-none focus-visible:ring-[3px] focus-visible:ring-ring/45 focus-visible:ring-offset-0',
    'disabled:pointer-events-none disabled:opacity-45',
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  ].join(' '),
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground shadow-e1 hover:shadow-e2 active:shadow-e1',
        destructive: 'bg-destructive text-destructive-foreground shadow-e1 hover:shadow-e2',
        outline: 'border border-border bg-surface/70 text-foreground backdrop-blur-sm',
        secondary: 'bg-secondary text-secondary-foreground',
        ghost: 'text-foreground/80 hover:text-foreground',
        link: 'text-primary underline-offset-4 hover:underline',
      },
      size: {
        default: 'h-9.5 px-4 py-2 has-[>svg]:px-3.5',
        sm: 'h-8 gap-1.5 px-3 text-[0.8125rem] has-[>svg]:px-2.5',
        lg: 'h-11 px-6 text-[0.9375rem] has-[>svg]:px-5',
        icon: 'size-9.5',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<'button'> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot : 'button'

  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
