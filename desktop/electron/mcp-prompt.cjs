'use strict';
// Skill installation is the primary local connection entry; no copied credentials.
function buildConnectPrompt({ skillPath, platform }) {
  const lines = [
    '请帮我安装并使用 Noteford Skill，读取我在应用中授权的资料。',
    `Skill 源目录：${JSON.stringify(skillPath)}`,
    '请先读取该目录的 SKILL.md，将完整目录安装到当前 AI 客户端实际支持的 Skill 目录；保留已有自定义文件，不覆盖无关内容。',
  ];
  if (platform === 'darwin') {
    lines.push(
      '如果当前 AI 无法访问这台 Mac 或不支持本机命令，请明确说明，不宣称已连接。',
      '安装后按 references/connection.md 使用 scripts/noteford.sh（或 scripts/noteford.py），ClientName 填真实 AI 产品名。通过公开发现记录连接运行中的 Noteford，无须复制令牌或手动配置 MCP。',
    );
  } else {
    lines.push(
      '如果当前 AI 无法访问这台 Windows 电脑或不支持本机命令，请明确说明，不宣称已连接。',
      '安装后按 references/connection.md 使用 scripts/noteford.ps1，ClientName 填真实 AI 产品名。通过公开发现记录连接运行中的 Noteford，无须复制令牌或手动配置 MCP。',
    );
  }
  lines.push(
    '首次访问请用户确认这是刚刚发起的连接，在 Noteford 原生弹窗中选择允许或拒绝。无需输入验证码；不代替用户确认，不连续刷新请求。',
    'paired: true 只证明授权完成。随后调用 list_libraries 和 get_status，只有实际成功且 read_only 为 true，才宣布连接可用。',
    '仅搜索和读取；不发送微信消息，不修改或删除资料。',
  );
  return lines.join('\n\n');
}
module.exports = { buildConnectPrompt };
