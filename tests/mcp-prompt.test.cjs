const test = require('node:test');
const assert = require('node:assert/strict');
const {buildConnectPrompt} = require('../desktop/electron/mcp-prompt.cjs');
test('Skill install prompt contains no credentials and requires user consent plus read verification', () => {
 const skillPath = 'C:/Program Files/Noteford/resources/skills/noteford-community';
 const text = buildConnectPrompt({skillPath, args: ['--pair', 'secret-token']});
 assert(text.includes(JSON.stringify(skillPath)));
 assert(!text.includes('secret-token'));
 for (const part of ['SKILL.md', 'noteford.ps1', '不代替用户确认', 'list_libraries', 'get_status', 'read_only']) assert(text.includes(part));
});
