import {useRef, type MouseEvent, type PointerEvent} from 'react'

// Native dialog backdrop events target the dialog itself; its padding does too.
export default function useBackdropDismiss(onClose: () => void, nativeDialog = true) {
  const startedOutside = useRef(false)
  function outside(event: MouseEvent<HTMLElement> | PointerEvent<HTMLElement>) {
    if (event.target !== event.currentTarget) return false
    if (!nativeDialog) return true
    const rect = event.currentTarget.getBoundingClientRect()
    return event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom
  }
  return {
    onPointerDown(event: PointerEvent<HTMLElement>) {
      startedOutside.current = event.button === 0 && outside(event)
    },
    onPointerCancel() { startedOutside.current = false },
    onClick(event: MouseEvent<HTMLElement>) {
      const dismiss = startedOutside.current && outside(event)
      startedOutside.current = false
      if (dismiss) onClose()
    },
  }
}
