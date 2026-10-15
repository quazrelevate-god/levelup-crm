import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'

import { SETTINGS_GROUPS } from '@/routes/nav'

/**
 * What you land on when you open Settings.
 *
 * Ten configuration screens with no explanation is a list of nouns; each one
 * here says what it actually governs, because "Actions" and "Assignment" mean
 * nothing until you have already learned them.
 */
export function SettingsOverviewPage() {
  return (
    <div className="flex flex-col gap-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-muted-foreground text-sm">
          How this workspace is shaped — its schema, its rules, and the platform it runs on.
        </p>
      </header>

      {SETTINGS_GROUPS.map((group) => (
        <section key={group.heading} className="flex flex-col gap-3">
          <h3 className="text-muted-foreground text-[0.6875rem] font-semibold tracking-wider uppercase">
            {group.heading}
          </h3>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {group.links.map((link) => {
              const Icon = link.icon
              return (
                <Link
                  key={link.to}
                  to={link.to}
                  className="state-layer bg-card border-border/60 shadow-neu-sm hover:shadow-e2 group focus-visible:ring-ring/45 flex items-start gap-3.5 rounded-2xl border p-4 transition-shadow duration-200 outline-none focus-visible:ring-[3px]"
                >
                  <span
                    aria-hidden="true"
                    className="bg-primary/12 text-primary flex size-9 shrink-0 items-center justify-center rounded-xl"
                  >
                    <Icon className="size-4.5" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1 text-sm font-semibold">
                      {link.label}
                      <ChevronRight
                        aria-hidden="true"
                        className="text-muted-foreground size-3.5 transition-transform duration-200 group-hover:translate-x-0.5"
                      />
                    </span>
                    <span className="text-muted-foreground mt-1 block text-[0.8125rem] leading-relaxed">
                      {link.description}
                    </span>
                  </span>
                </Link>
              )
            })}
          </div>
        </section>
      ))}
    </div>
  )
}
