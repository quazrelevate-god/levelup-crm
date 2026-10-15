import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A minimal modal built on `<dialog>`.
 *
 * The browser element gives focus trapping, Escape-to-close and the top layer
 * for free — all of which a hand-rolled overlay gets wrong.
 */
interface DialogProps {
  readonly open: boolean
  readonly onClose: () => void
  readonly title: string
  readonly description?: string
  readonly children: React.ReactNode
  readonly footer?: React.ReactNode
  /**
   * Extra classes for the dialog element. The field drawer and the permission
   * matrix need more than the default `max-w-lg`; everything else is happy
   * with it, so the width stays a caller's decision rather than a new variant.
   */
  readonly className?: string
}

function Dialog({ open, onClose, title, description, children, footer, className }: DialogProps) {
  const ref = React.useRef<HTMLDialogElement>(null)

  React.useEffect(() => {
    const element = ref.current
    if (!element) return

    if (open && !element.open) {
      element.showModal()
    } else if (!open && element.open) {
      element.close()
    }
  }, [open])

  // Unmounted while closed, rather than hidden.
  //
  // A closed `<dialog>` keeps its children in the DOM and in the accessibility
  // tree, so every label inside one stays addressable. Two dialogs on a screen
  // with a "Name" field each — the lead quick-add and the save-filter form —
  // make "the Name box" ambiguous for a screen reader and for any by-label
  // query, even though the user can see only one of them. Unmounting also means
  // a form is genuinely reset when it reopens rather than holding its last
  // draft.
  if (!open) return null

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      className={cn(
        'bg-card text-foreground shadow-e3 w-full max-w-lg rounded-2xl border p-0',
        // A modal <dialog> is centred by the user agent's own `margin: auto`,
        // which Tailwind's preflight resets to 0 — so without this every dialog
        // pins itself to the top-left corner of the viewport.
        'm-auto max-h-[85vh] overflow-y-auto',
        // The backdrop blurs the page behind rather than only dimming it, which
        // is what makes the dialog read as above everything else.
        'open:animate-in backdrop:bg-black/35 backdrop:backdrop-blur-sm',
        className,
      )}
    >
      <div className="flex flex-col gap-4 p-6">
        <header className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold">{title}</h2>
          {description ? <p className="text-muted-foreground text-sm">{description}</p> : null}
        </header>
        <div className="flex flex-col gap-4">{children}</div>
        {footer ? <footer className="flex justify-end gap-2">{footer}</footer> : null}
      </div>
    </dialog>
  )
}

export { Dialog }
