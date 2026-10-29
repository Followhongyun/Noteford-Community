import {useLayoutEffect, useMemo, useReducer} from 'react'

// Keep view snapshots within one authenticated library, never browser history or disk.
export default function useNavigationHistory<T>(scope: string, key: string, value: T, restore: (value: T) => void) {
  const store = useMemo(() => ({entries: [] as {key: string; value: T}[], cursor: -1}), [scope])
  const [, render] = useReducer((value: number) => value + 1, 0)
  useLayoutEffect(() => {
    if (store.entries[store.cursor]?.key === key) {
      store.entries[store.cursor].value = value
      return
    }
    store.entries.splice(store.cursor + 1)
    store.entries.push({key, value})
    if (store.entries.length > 40) store.entries.shift()
    store.cursor = store.entries.length - 1
    render()
  }, [store, key, value])
  const move = (direction: number) => {
    const cursor = store.cursor + direction
    if (cursor < 0 || cursor >= store.entries.length) return
    store.cursor = cursor
    restore(store.entries[cursor].value)
    render()
  }
  return {canBack: store.cursor > 0, canForward: store.cursor < store.entries.length - 1, back: () => move(-1), forward: () => move(1)}
}
