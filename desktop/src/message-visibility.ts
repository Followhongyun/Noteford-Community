import type {Message} from './types'

export interface MessageVisibility { hideRevoke: boolean; hidePat: boolean }
export const visibilityKey = 'noteford.message-visibility.v1'
export const defaultVisibility: MessageVisibility = {hideRevoke: false, hidePat: false}

export function loadVisibility(key = visibilityKey, inheritLegacy = false): MessageVisibility {
  try {
    const value = JSON.parse(localStorage.getItem(key) || (inheritLegacy ? localStorage.getItem(visibilityKey) : null) || '{}')
    return {hideRevoke: value?.hideRevoke === true, hidePat: value?.hidePat === true}
  } catch { return {...defaultVisibility} }
}

export function isMessageHidden(message: Message, preferences: MessageVisibility): boolean {
  const kind = message.type.kind
  if (preferences.hideRevoke && kind === 'revoke') return true
  if (preferences.hidePat && kind === 'pat') return true
  if (kind !== 'system') return false
  const text = message.text.trim()
  return (preferences.hideRevoke && /^.*撤回了?一条消息[。！]?$/.test(text))
    || (preferences.hidePat && /^(?:.+拍了拍.+|\[拍一拍\])$/.test(text))
}
