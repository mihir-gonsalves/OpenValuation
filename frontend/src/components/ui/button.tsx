import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

export function Button({ className, type = 'button', ...props }: ComponentProps<'button'>) {
  return (
    <button
      type={type}
      className={cn(
        'inline-flex items-center px-3 py-1 gap-1.5 bg-popover border rounded-md text-sm [&_svg]:size-4 transition-colors hover:cursor-pointer hover:bg-accent hover:border-primary/75 hover:ring-1 hover:ring-ring/50 hover:text-accent-foreground focus-visible:outline-none focus-visible:bg-accent focus-visible:border-primary/75 focus-visible:ring-1 focus-visible:ring-ring/50 focus-visible:text-accent-foreground',
        className,
      )}
      {...props}
    />
  )
}
