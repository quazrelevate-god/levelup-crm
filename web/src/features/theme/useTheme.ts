/**
 * Light / dark, remembered per browser.
 *
 * The dark tokens existed from the start but nothing ever applied the class,
 * so the whole dark column was dead code. This is the thing that turns it on.
 *
 * `system` is the default and is a real third state, not a synonym for light:
 * it keeps following the OS after the fact, which a resolved-once boolean
 * cannot do.
 */

import { useCallback, useEffect, useState } from 'react'

export type ThemePreference = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'crm.theme'

export function readStoredPreference(): ThemePreference {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw
  } catch {
    // Private windows and blocked site data both throw here. A missing
    // preference is not an error state — it is the default.
  }
  return 'system'
}

export function applyTheme(preference: ThemePreference): void {
  const dark =
    preference === 'dark' ||
    (preference === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
}

export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(readStoredPreference)

  useEffect(() => {
    applyTheme(preference)
    try {
      localStorage.setItem(STORAGE_KEY, preference)
    } catch {
      // Not being able to remember the choice is not a reason to refuse it.
    }
  }, [preference])

  // Only while following the system: a chosen theme must not move underfoot.
  useEffect(() => {
    if (preference !== 'system') return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => applyTheme('system')
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [preference])

  const cycle = useCallback(() => {
    setPreference((current) =>
      current === 'light' ? 'dark' : current === 'dark' ? 'system' : 'light',
    )
  }, [])

  return { preference, setPreference, cycle }
}
