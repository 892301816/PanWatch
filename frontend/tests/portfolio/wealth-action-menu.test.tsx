import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import WealthActionMenu from '@/components/WealthActionMenu'

afterEach(cleanup)
beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  HTMLElement.prototype.scrollIntoView = vi.fn()
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => {}
  HTMLElement.prototype.releasePointerCapture = () => {}
})

describe('wealth secondary actions', () => {
  it('shows named actions and calls the selected handler without opening the parent row', () => {
    const onRowClick = vi.fn()
    const onEdit = vi.fn()
    render(
      <div onClick={onRowClick}>
        <WealthActionMenu
          label="贵州茅台更多操作"
          actions={[{ label: '编辑持仓', onSelect: onEdit }]}
        />
      </div>,
    )

    fireEvent.keyDown(screen.getByRole('button', { name: '贵州茅台更多操作' }), { key: 'Enter', code: 'Enter' })
    fireEvent.click(screen.getByRole('menuitem', { name: '编辑持仓' }))

    expect(onEdit).toHaveBeenCalledTimes(1)
    expect(onRowClick).not.toHaveBeenCalled()
    expect(screen.queryByRole('menu') === null).toBe(true)
  })

  it('protects disabled actions and closes the menu on Escape', () => {
    const onDelete = vi.fn()
    const onDetails = vi.fn()
    render(
      <WealthActionMenu actions={[
        { label: '删除持仓', onSelect: onDelete, disabled: true, destructive: true },
        { label: '查看详情', onSelect: onDetails },
      ]} />,
    )
    const trigger = screen.getByRole('button', { name: '更多' })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
    const disabledItem = screen.getByRole('menuitem', { name: '删除持仓' })
    expect(disabledItem.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(disabledItem)
    expect(screen.queryByRole('menu') !== null).toBe(true)
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape', code: 'Escape' })
    expect(screen.queryByRole('menu') === null).toBe(true)
    expect(onDelete).not.toHaveBeenCalled()
    expect(onDetails).not.toHaveBeenCalled()
  })
})
