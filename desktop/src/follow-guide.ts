// Claim the first visit, not the completion button: leaving the page or restarting
// must not turn an already-seen introduction back into a pending task.
export function claimFirstFollowVisit(storage: Pick<Storage, 'getItem' | 'setItem'>, input: {
  accountId: string
  libraryIds: string[]
  legacyWorkspace: boolean
  hasPriorFollows: boolean
}): boolean {
  const key = `noteford.mac-follow-guide.v2:${input.accountId}`
  const seen = storage.getItem(key) === 'seen'
    || input.hasPriorFollows
    || input.libraryIds.some(id =>
      storage.getItem(`noteford.mac-follow-guide.v1:${input.accountId}:${id}`) === 'done'
      || (input.legacyWorkspace && storage.getItem(`noteford.mac-follow-guide.v1:${id}`) === 'done'))
  storage.setItem(key, 'seen')
  return !seen
}
