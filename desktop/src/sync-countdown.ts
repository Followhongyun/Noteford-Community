export function syncCountdown(nextAt: string | null | undefined, now: number): string {
  const deadline = nextAt ? Date.parse(nextAt) : NaN
  if (!Number.isFinite(deadline)) return '--:--'
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds / 60) % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${hours ? `${hours}:` : ''}${pad(minutes)}:${pad(seconds % 60)}`
}
