import * as React from 'react'

import { cn } from '@/lib/utils'
import { fieldClasses } from '@/components/ui/input'

function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(fieldClasses, 'flex min-h-16 py-2', className)}
      {...props}
    />
  )
}

export { Textarea }
