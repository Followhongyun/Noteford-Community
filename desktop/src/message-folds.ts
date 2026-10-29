import type { Message } from './types'

export interface MessageGroup {
  messages: Message[]
  label?: string
}

// Recognize complete, known WeChat emoji tokens, not arbitrary bracketed prose.
const emojiNames = new Set(('微笑 撇嘴 色 发呆 得意 流泪 害羞 闭嘴 睡 大哭 尴尬 发怒 调皮 呲牙 惊讶 难过 酷 冷汗 抓狂 吐 偷笑 可爱 白眼 傲慢 饥饿 困 惊恐 流汗 憨笑 悠闲 大兵 奋斗 咒骂 疑问 嘘 晕 疯了 衰 骷髅 敲打 再见 擦汗 抠鼻 鼓掌 糗大了 坏笑 左哼哼 右哼哼 哈欠 鄙视 委屈 快哭了 阴险 亲亲 吓 可怜 菜刀 西瓜 啤酒 篮球 乒乓 咖啡 饭 猪头 玫瑰 凋谢 嘴唇 爱心 心碎 蛋糕 闪电 炸弹 刀 足球 瓢虫 便便 月亮 太阳 礼物 拥抱 强 弱 握手 胜利 抱拳 勾引 拳头 差劲 爱你 NO OK 爱情 飞吻 跳跳 发抖 怄火 转圈 磕头 回头 跳绳 投降 激动 乱舞 献吻 左太极 右太极 加油 胜利 旺柴 吃瓜 机智 捂脸 破涕为笑 耶 奸笑 皱眉 红包 发 抓狂 无语 汗 天啊 社会社会 加油加油 打脸 哇 翻白眼 嘿哈 666 让我看看 叹气 苦涩 裂开 失望 合十 烟花 爆竹 庆祝 福 点赞').split(' '))

export function isEmojiOnly(message: Message): boolean {
  if (['emoji', 'gif'].includes(message.type.kind)) return true
  if (message.type.kind !== 'text') return false
  const text = message.text.trim()
  if (!text) return false
  const remainder = text.replace(/\[([^\[\]]+)\]/g, (token, name: string) => emojiNames.has(name) ? '' : token).replace(/\s/g, '')
  if (!remainder) return true
  // Unicode emoji sequences (including skin tones, ZWJ and flag pairs).
  return /^(?:(?:\p{Extended_Pictographic}(?:\uFE0F|\p{Emoji_Modifier})?(?:\u200D\p{Extended_Pictographic}(?:\uFE0F|\p{Emoji_Modifier})?)*)|\p{Regional_Indicator}{2})+$/u.test(remainder)
}

function foldKind(message: Message, now: number): string | undefined {
  if (message.type.kind === 'system') {
    const text = message.text.trim()
    if (/^.+邀请.+加入了群聊[。！]?$/.test(text)
      || /^.+与群里其他人都不是朋友关系，请注意隐私安全[。！]?$/.test(text)) return '条群通知'
  }
  if (isEmojiOnly(message)) return '个表情'
  if (message.type.kind === 'transfer' && message.payment?.recipient_is_self === false) return '笔转账'
  if (message.type.kind === 'red_envelope') {
    const expires = message.payment?.expires_at
    if ((expires && Number.isFinite(expires) && expires > message.timestamp && expires <= now)
      || message.payment?.recipient_is_self === false
      || ['received', 'exhausted', 'expired'].includes(message.payment?.local_state || '')) return '个红包'
  }
  return undefined
}

// Count consecutive eligible messages together, independent of their kinds.
// Only presentation changes; source records and search results remain intact.
export function groupMessages(messages: Message[], enabled = true, now = Date.now() / 1000): MessageGroup[] {
  const groups: MessageGroup[] = []
  let pending: Message[] = []
  const counts = new Map<string, number>()
  const flush = () => {
    if (pending.length >= 3 || (counts.get('条群通知') || 0) >= 2) groups.push({messages: pending, label: '已折叠：' + [...counts].map(([kind, count]) => `${count} ${kind}`).join(' · ')})
    else groups.push(...pending.map(message => ({messages: [message]})))
    pending = []
    counts.clear()
  }
  for (const message of messages) {
    const kind = enabled ? foldKind(message, now) : undefined
    if (kind) {
      pending.push(message)
      counts.set(kind, (counts.get(kind) || 0) + 1)
    } else {
      flush()
      groups.push({messages: [message]})
    }
  }
  flush()
  return groups
}
