import { AlertTriangle, RotateCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/api/client'
import { errorCopy } from '@/lib/errors'

interface ErrorMessageProps {
  error: unknown
  onRetry?: () => void
}

export function ErrorMessage({ error, onRetry }: ErrorMessageProps) {
  const { title, body } =
    error instanceof ApiError
      ? errorCopy(error.code)
      : { title: 'Something went wrong', body: 'An unexpected error occurred. Please try again.' }

  // 4xx errors won't resolve on retry, only offer retry for server/network issues.
  const canRetry = onRetry && !(error instanceof ApiError && error.isClientError)

  return (
    <div className="flex flex-col mx-auto max-w-md items-center py-20 gap-3 text-center">
      <AlertTriangle className="text-warning size-6" />
      <h2 className="font-semibold text-foreground">{title}</h2>
      <p className="text-sm text-muted-foreground">{body}</p>
      {canRetry && (
        <Button onClick={onRetry} className="mt-9">
          <RotateCw />
          Try again
        </Button>
      )}
    </div>
  )
}
