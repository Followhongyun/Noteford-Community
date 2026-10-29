export type TextPart = {text: string; url?: string}

export function webUrl(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && url.hostname ? url.href : undefined
  } catch { return undefined }
}

// Keep the original text; only attach destinations to recognized web addresses.
export function splitLinks(text: string): TextPart[] {
  const pattern = /https?:\/\/[^\s<>"'`，。！？；：、（）【】《》“”‘’,]+|(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]+)(?::\d{1,5})?(?:[/?#][^\s<>"'`，。！？；：、（）【】《》“”‘’,]*)?/gi
  const parts: TextPart[] = []
  let end = 0
  for (const match of text.matchAll(pattern)) {
    const start = match.index!
    // Do not link fragments inside email addresses, file URLs or identifiers.
    if (start > 0 && /[a-z0-9_@/.-]/i.test(text[start-1])) continue
    let label = match[0].replace(/[.!?;:]+$/, '')
    for (const [open, close] of [['(', ')'], ['[', ']'], ['{', '}']]) {
      while (label.endsWith(close) && label.split(close).length > label.split(open).length) {
        label = label.slice(0, -1)
      }
    }
    label = label.replace(/[.!?;:]+$/, '')
    const url = webUrl(/^https?:\/\//i.test(label) ? label : `https://${label}`)
    if (!url) continue
    if (start > end) parts.push({text: text.slice(end, start)})
    parts.push({text: label, url})
    end = start + label.length
  }
  if (end < text.length) parts.push({text: text.slice(end)})
  return parts
}
