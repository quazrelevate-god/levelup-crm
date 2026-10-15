import { Monitor, Moon, Sun } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useTheme } from '@/features/theme/useTheme'

const LABELS = {
  light: 'Theme: light',
  dark: 'Theme: dark',
  system: 'Theme: follow system',
} as const

export function ThemeToggle() {
  const { preference, cycle } = useTheme()
  const Icon = preference === 'light' ? Sun : preference === 'dark' ? Moon : Monitor

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={cycle}
      // The icon alone is the whole control, so the name has to live here.
      aria-label={LABELS[preference]}
      title={LABELS[preference]}
    >
      <Icon aria-hidden="true" />
    </Button>
  )
}
