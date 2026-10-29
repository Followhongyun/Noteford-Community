'use strict';
// First-party renderer controls only. Avatars, message media, brand assets,
// native window chrome, input widgets and media controls retain their sources.
const fs = require('node:fs');
const path = require('node:path');
const lucide = require('lucide-react');
const root = path.resolve(__dirname, '../src');
const errors = [];
let files = 0, uses = 0;
function scan(dir) {
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) { scan(file); continue; }
    if (!/\.(tsx|css)$/.test(file)) continue;
    files++;
    const source = fs.readFileSync(file, 'utf8');
    const label = path.relative(root, file);
    uses += (source.match(/<Icon\b/g) || []).length;
    if (/from\s+['"]lucide-react['"]/.test(source) && label !== 'components\\Icon.tsx' && label !== 'components/Icon.tsx') errors.push(`${label}: use the shared Icon component`);
    if (/<(?:svg|path|polygon|polyline|circle)\b/.test(source)) errors.push(`${label}: inline icon geometry`);
    if (/[⌖▶▼▲◀✕×⚙🔍📁✓✗↗←→★☆☰＋]/u.test(source)) errors.push(`${label}: possible Unicode control icon; use Lucide`);
    if (/content\s*:\s*['"][^'"\s]+['"]/.test(source)) errors.push(`${label}: CSS content glyph`);
    if (/from\s+['"](?:react-icons|@heroicons|@fortawesome)/.test(source)) errors.push(`${label}: another icon library`);
  }
}
scan(root);
const wrapper = fs.readFileSync(path.join(root, 'components/Icon.tsx'), 'utf8');
const imported = wrapper.match(/import\s*{([\s\S]*?)}\s*from\s*['"]lucide-react['"]/)[1];
const names = imported.split(',').map(s => s.trim()).filter(s => s && !s.startsWith('type '));
for (const name of names) if (!lucide[name]) errors.push(`Unknown Lucide export: ${name}`);
if (errors.length) { console.error(errors.join('\n')); process.exitCode = 1; }
else console.log(`Icon check: ${files} renderer files, ${uses} shared icon usages, ${names.length} verified Lucide exports; 0 violations.`);
