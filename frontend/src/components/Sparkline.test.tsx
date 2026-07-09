import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { Sparkline } from './Sparkline'

describe('Sparkline', () => {
  it('draws a single polyline for contiguous values', () => {
    const { container } = render(<Sparkline values={[1, 2, 3, 4]} />)
    expect(container.querySelectorAll('polyline')).toHaveLength(1)
  })

  it('breaks the line into segments around null gaps', () => {
    const { container } = render(<Sparkline values={[1, 2, null, 4, 5]} />)
    expect(container.querySelectorAll('polyline')).toHaveLength(2)
  })

  it('renders a dashed baseline when there are fewer than two points', () => {
    const { container } = render(<Sparkline values={[null, 3]} />)
    expect(container.querySelector('polyline')).toBeNull()
    expect(container.querySelector('line')).not.toBeNull()
  })

  it('renders circles for isolated points surrounded by nulls', () => {
    const { container } = render(<Sparkline values={[1, null, 3, null, 5]} />)
    expect(container.querySelectorAll('polyline')).toHaveLength(0)
    const circles = container.querySelectorAll('circle')
    // Three isolated points each get a circle, plus the end-dot circle
    expect(circles.length).toBeGreaterThanOrEqual(3)
  })
})
