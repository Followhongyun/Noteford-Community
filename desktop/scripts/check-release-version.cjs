'use strict';
const fs = require('node:fs');
const path = require('node:path');
function check(root = path.resolve(__dirname, '../..'), candidateVersion) {
  const read = file => fs.readFileSync(path.join(root,file),'utf8');
  const policy = JSON.parse(read('desktop/release-policy.json'));
  const pkg = JSON.parse(read('desktop/package.json'));
  const lock = JSON.parse(read('desktop/package-lock.json'));
  const versions = [pkg.version,lock.version,lock.packages[''].version,
    read('pyproject.toml').match(/^version\s*=\s*"([^"]+)"/m)?.[1],
    read('src/noteford/__init__.py').match(/__version__\s*=\s*"([^"]+)"/)?.[1]];
  if (candidateVersion !== undefined) versions.push(candidateVersion);
  if (versions.some(v=>!v || v!==pkg.version)) throw Error('桌面、锁文件、Python 和候选版本必须一致');
  if (policy.frozen && versions.some(v=>v!==policy.version)) throw Error(`推广前版本冻结为 ${policy.version}；须维护者明确确认推广已发布后才能解除`);
  return pkg.version;
}
if (require.main===module) console.log('Release version: '+check());
module.exports={check};
