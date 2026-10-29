'use strict';
// Maintained in Muxuno, vendored unchanged into each product. No runtime cross-product dependency.
const MODULE_VERSION = '1.1.0';
const identifier = value => typeof value === 'string' && /^[a-zA-Z0-9_-]+$/.test(value);
const tomlString = value => JSON.stringify(value).replace(/\u007f/g, '\\u007f');

function renderClientConfigs(serverName, server) {
  if (!identifier(serverName)) throw new Error('Invalid MCP server name');
  if (!server || typeof server !== 'object' || Array.isArray(server)) throw new Error('Invalid MCP configuration');
  const entry = {};
  const lines = [`[mcp_servers.${serverName}]`];
  if (typeof server.url === 'string') {
    const url = new URL(server.url);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || url.search || url.hash) throw new Error('Expected a loopback MCP endpoint');
    if (server.command || server.args || server.env) throw new Error('Mixed MCP transports');
    entry.url = server.url;
    lines.push(`url = ${tomlString(server.url)}`);
    if (server.headers) {
      entry.headers = stringMap(server.headers);
      lines.push(`http_headers = { ${Object.entries(entry.headers).map(([key, value]) => `${tomlString(key)} = ${tomlString(value)}`).join(', ')} }`);
    }
  } else {
    if (typeof server.command !== 'string' || !server.command.trim() || !Array.isArray(server.args) || server.args.some(arg => typeof arg !== 'string')) throw new Error('Invalid stdio command');
    if (server.headers) throw new Error('Mixed MCP transports');
    entry.command = server.command;
    entry.args = [...server.args];
    lines.push(`command = ${tomlString(entry.command)}`, `args = [${entry.args.map(tomlString).join(', ')}]`);
    if (server.env && Object.keys(server.env).length) {
      entry.env = stringMap(server.env);
      lines.push('', `[mcp_servers.${serverName}.env]`, ...Object.entries(entry.env).map(([key, value]) => `${tomlString(key)} = ${tomlString(value)}`));
    }
  }
  return { json: JSON.stringify({ mcpServers: { [serverName]: entry } }, null, 2), toml: lines.join('\n') };
}

function stringMap(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.values(value).some(item => typeof item !== 'string')) throw new Error('Expected string configuration values');
  return { ...value };
}

function createConnectionPrompt({ product, serverName, server, connectionFile, skillPath, authorizationSteps = [], verification, legacyServerNames = [] }) {
  if (typeof product !== 'string' || !product.trim() || !identifier(serverName) || typeof verification !== 'string' || !verification.trim()) throw new Error('Incomplete product setup');
  if (Boolean(server) === Boolean(connectionFile)) throw new Error('Specify one configuration source');
  if (!Array.isArray(authorizationSteps) || authorizationSteps.some(step => typeof step !== 'string') || legacyServerNames.some(name => !identifier(name))) throw new Error('Invalid setup steps');
  const lines = [
    `请帮我接入本机 ${product}，在当前 AI 客户端保存名为 ${serverName} 的用户级 MCP 配置，让新聊天也能使用。`,
    '先确认当前客户端支持对应的本地 MCP。备份配置，仅新增或更新这个产品的连接，保留其他服务、设置和用户定制；不要打印凭据，不要上传本机连接文件。',
  ];
  if (legacyServerNames.length) lines.push(`如已有旧名称 ${legacyServerNames.join('、')}，先核对是否指向同一产品和同一授权；不能确认时保留并说明，不能同时注册两份重复连接或擅自替换授权。`);
  if (server) {
    const config = renderClientConfigs(serverName, server);
    lines.push('', '支持 mcpServers JSON 的客户端：', '```json', config.json, '```', 'Codex 用户配置（~/.codex/config.toml）：', '```toml', config.toml, '```');
  } else {
    if (typeof connectionFile !== 'string' || !connectionFile.trim()) throw new Error('Invalid connection file');
    lines.push(`读取本机配置文件 ${JSON.stringify(connectionFile)}。若文件提供 url 和 token，使用它们配置 Streamable HTTP MCP；若文件声明需要配对，先按以下产品授权步骤取得此连接的凭据，再保存配置，不猜测或绕过缺失的 token。Authorization 为 Bearer 加实际 token。Codex 使用 url 和 http_headers，其他客户端按其真实配置格式填写。不要把 token 写入聊天、Skill 或公开日志。`);
  }
  if (skillPath) lines.push('', `客户端支持 Skill 时，将本机 ${JSON.stringify(skillPath)} 的完整 Skill 目录安装到该客户端的用户级 Skill 目录；已有自定义内容先比较并保留，不直接覆盖。Skill 是可选操作指南，不能代替 MCP 配置；不支持 Skill 时使用 MCP 自带说明。`);
  lines.push('', '按客户端要求重新加载 MCP；如果当前聊天工具列表没有刷新，在新聊天继续验证，不反复覆盖配置。');
  lines.push(...authorizationSteps);
  lines.push(`只读验证：${verification}`, '只有真实工具调用成功且返回目标产品的预期状态，才能报告已连接；安装配置、工具已列出或配对已确认都不是最终验收。', `再新建一个空白聊天，发送“检查 ${product} 连接，只读取状态，不修改数据”，确认无需再次安装或授权。`, '各产品独立运行，只访问本机服务；请保持目标产品运行。返回给云端 AI 的内容可能由其模型服务处理，本地 MCP 不等于本地模型。', '客户端不支持本机 MCP 或不能修改配置时，说明具体限制并提供手动配置，不声称已安装；不新增公网隧道或云端中转。');
  return lines.join('\n');
}

module.exports = { MODULE_VERSION, renderClientConfigs, createConnectionPrompt };
