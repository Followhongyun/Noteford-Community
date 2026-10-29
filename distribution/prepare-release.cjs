'use strict';
// Run only after notarization and stapling of the final App and DMG. The ZIP
// must contain that same stapled App; never regenerate either artifact later.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
async function describe(root, name) {
  const file = path.join(root, name), stat = await fs.promises.stat(file);
  const hash = createHash('sha512');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return { name, size: stat.size, sha512: hash.digest('hex') };
}
async function prepare(root, version) {
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw Error('Invalid version');
  // User-facing installers use the system name. Keep the updater ZIP's existing
  // architecture name, which the deployed origin validates, and accept old DMGs.
  const macDmg = `Noteford-${version}-macOS.dmg`;
  const dmg = fs.existsSync(path.join(root, macDmg)) ? macDmg : `Noteford-${version}-arm64.dmg`;
  const artifacts = await Promise.all([
    describe(root, `Noteford-${version}-arm64.zip`), describe(root, dmg),
  ]);
  const zip = artifacts[0];
  const base64 = a => Buffer.from(a.sha512, 'hex').toString('base64');
  const metadata = `version: ${version}\nfiles:\n` + artifacts.map(a =>
    `  - url: ${a.name}\n    sha512: ${base64(a)}\n    size: ${a.size}\n`).join('')
    + `path: ${zip.name}\nsha512: ${base64(zip)}\nreleaseDate: '${new Date().toISOString()}'\n`;
  await fs.promises.writeFile(path.join(root, 'latest-mac.yml'), metadata, { flag: 'wx' });
  artifacts.push(await describe(root, 'latest-mac.yml'));
  await fs.promises.writeFile(path.join(root, 'release.json'), JSON.stringify({ version, artifacts }, null, 2) + '\n', { flag: 'wx' });
}
if (require.main === module) prepare(path.resolve(process.argv[2]), process.argv[3]).catch(error => {
  console.error(error.message); process.exitCode = 1;
});
module.exports = { prepare };
