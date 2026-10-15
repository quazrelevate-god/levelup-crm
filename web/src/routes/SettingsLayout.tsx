import { Link, Outlet, useLocation } from 'react-router-dom'

import { SETTINGS_GROUPS, isActive } from '@/routes/nav'
import { cn } from '@/lib/utils'

/**
 * The settings shell.
 *
 * Everything that configures a workspace, plus the deployment's own status
 * page, lives under one route so the main sidebar can carry a single entry
 * instead of ten. The secondary rail is what replaces them, and it only exists
 * once you have deliberately opened this door.
 */
export function SettingsLayout() {
  const location = useLocation()

  return (
    <div className="flex flex-col gap-6 lg:flex-row lg:gap-8">
      <nav aria-label="Settings" className="lg:w-[228px] lg:shrink-0">
        {/* Horizontal and scrollable below lg, where a rail would eat the page. */}
        <div className="flex gap-4 overflow-x-auto pb-2 lg:flex-col lg:gap-5 lg:overflow-visible lg:pb-0">
          {SETTINGS_GROUPS.map((group) => (
            <div key={group.heading} className="flex min-w-max flex-col gap-0.5 lg:min-w-0">
              <p className="text-muted-foreground/70 px-3 pb-1 text-[0.6875rem] font-semibold tracking-wider uppercase">
                {group.heading}
              </p>
              <div className="flex gap-1 lg:flex-col lg:gap-0.5">
                {group.links.map((link) => {
                  const Icon = link.icon
                  const active = isActive(location.pathname, link.to)
                  return (
                    <Link
                      key={link.to}
                      to={link.to}
                      aria-current={active ? 'page' : undefined}
                      className={cn(
                        'state-layer flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium whitespace-nowrap outline-none',
                        'focus-visible:ring-ring/45 transition-colors duration-150 focus-visible:ring-[3px]',
                        active
                          ? 'bg-primary/12 text-primary'
                          : 'text-muted-foreground hover:text-foreground',
                      )}
                    >
                      <Icon aria-hidden="true" className="size-4 shrink-0" />
                      {link.label}
                    </Link>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </nav>

      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  )
}
