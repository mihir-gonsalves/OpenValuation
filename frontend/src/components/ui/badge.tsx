import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

/**
 * Small inline pill used for data-quality notes - warning and neutral tags
 * like "fallback". The warning variant is focusable/hoverable because it
 * usually triggers a tooltip, the neutral variant is purely decorative.
 */
const VARIANTS = {
  warning: 'bg-warning-surface border-warning/50 text-warning-foreground focus-visible:outline-none',
  neutral: 'bg-secondary text-muted-foreground focus-visible:outline-none',
}

interface BadgeProps extends ComponentProps<'span'> {
  variant?: keyof typeof VARIANTS
}

export function Badge({ variant = 'warning', className, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'w-fit px-1.5 py-0.5 border rounded-sm text-[0.6875rem] cursor-default leading-none',
        VARIANTS[variant],
        className,
      )}
      {...props}
    />
  )
}
