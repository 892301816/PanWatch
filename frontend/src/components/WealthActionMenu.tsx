import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { MoreHorizontal } from 'lucide-react'

export interface WealthAction {
  label: string
  onSelect: () => void
  destructive?: boolean
  disabled?: boolean
}

interface WealthActionMenuProps {
  label?: string
  triggerText?: string
  actions: WealthAction[]
}

/** Keep secondary actions discoverable without making every table row a toolbar. */
export default function WealthActionMenu({ label = '更多', triggerText = '更多', actions }: WealthActionMenuProps) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={label}
          className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-lg px-3 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          onPointerDown={event => event.stopPropagation()}
          onClick={event => event.stopPropagation()}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
          {triggerText}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          aria-label={label}
          className="z-[70] max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-40 overflow-y-auto rounded-xl border border-border bg-popover p-1.5 text-popover-foreground shadow-lg outline-none"
          onPointerDown={event => event.stopPropagation()}
          onClick={event => event.stopPropagation()}
        >
          {actions.map((action, index) => (
            <DropdownMenu.Item
              key={`${action.label}-${index}`}
              disabled={action.disabled}
              className={`flex min-h-11 cursor-pointer select-none items-center rounded-lg px-3 text-sm outline-none data-[highlighted]:bg-accent data-[disabled]:pointer-events-none data-[disabled]:opacity-40 ${action.destructive ? 'text-destructive' : ''}`}
              onSelect={() => {
                if (!action.disabled) action.onSelect()
              }}
            >
              {action.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
