export interface PermissionCard {
  id: string
  title: string
  detail: string
  restartNote: string
  gapNote?: string
  identityNote?: string
  canRestart?: boolean
  required: boolean
  optionalLabel: string
  status: 'granted' | 'not_granted' | 'restart_required'
  actionLabel: string
}

export interface PermissionGuide {
  revision?: number
  active: boolean
  loading: boolean
  title: string
  intro: string
  cards: PermissionCard[]
  canContinue: boolean
  continueLabel: string
  hint: string
  steps: { id: string; label: string }[]
}

export const GRANT_UI_TIMEOUT_MS: number
export const SETTINGS_GUIDANCE: string
export function reduceGrantUi(
  state: { grantingId: string | null; notice: string; noticeId?: string | null } | null,
  event: { type: string; id?: string; status?: string; guidance?: string; openedSettings?: boolean },
): { grantingId: string | null; notice: string; noticeId: string | null }
export function loadingPermissionGuide(): PermissionGuide
export function applyOnboardingKey(input: {
  platform: string
  step: string
  key: string
  canContinue: boolean
  connectCanContinue?: boolean
}): { step: string; handled: boolean; changed: boolean }
