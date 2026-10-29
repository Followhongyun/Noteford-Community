export interface WechatConnectCard {
  pageTitle: string
  intro: string
  title: string
  detail: string
  risk: string
  actionLabel: string
  status: 'ready' | 'waiting' | 'captured' | 'verifying' | 'done' | 'failed' | 'blocked' | 'error'
  canContinue: boolean
  canStart: boolean
  busy: boolean
}

export function presentConnectCard(snapshot: object | null | undefined): WechatConnectCard
