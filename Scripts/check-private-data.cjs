#!/usr/bin/env node
// Local Git guard. Never print data contents or contact the network.
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const git = (...args) => execFileSync('git', args, {encoding:'utf8'});
const privatePath = name => /^(data|archive|secrets|product-accounts|build|out|dist|node_modules|work|tmp)(\/|$)|^config\.local\.json$|(^|\/)\.env($|\.)/i.test(name);
const sourceRoots = new Set([".cursor", ".gitattributes", ".githooks", ".github", ".gitignore", ".npmignore", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "NOTICE.md", "README.md", "README.en.md", "SECURITY.md", "Scripts", "config.example.json", "desktop", "distribution", "docs", "native", "packaging", "pyproject.toml", "requirements-build-mac.txt", "requirements-build.txt", "scripts", "src", "tests"]);
const localRoots = new Set([...sourceRoots, '.git', '.DS_Store', 'Data', 'node_modules', '.venv', '.venv-build', '.pytest_cache', '.pytest-tmp', 'build', '源码', '应用']);
function checkLayout() {
  for (const file of git('ls-files','-z').split('\0').filter(Boolean)) {
    if (!sourceRoots.has(file.split('/')[0])) throw Error('源码顶层目录未登记：' + file.split('/')[0] + '。请按开发指南放入已有目录，禁止直接扩散根目录。');
  }
  for (const name of fs.readdirSync('.')) {
    if (process.platform === 'win32' && ['data','libraries','secrets','config.local.json'].includes(name)) continue;
    if (!localRoots.has(name)) throw Error('项目根目录存在未登记文件：' + name + '。临时文件和归档应放仓库外。');
  }
}
function check(tree) {
  const files = tree ? git('ls-tree','-r','--name-only','-z',tree) : git('ls-files','-z');
  if (files.split('\0').some(privatePath)) throw Error('拒绝操作：Git 索引或待推送历史包含私有数据、本地配置或生成目录。请先移出 Git 跟踪，保留本机文件。');
}
try {
  check();
  checkLayout();
  if (process.argv.includes('--install')) {
    git('config','core.hooksPath','.githooks');
    console.log('已安装目录与私有数据保护钩子。');
  }
  if (process.argv.includes('--push')) {
    for (const line of fs.readFileSync(0,'utf8').trim().split('\n').filter(Boolean)) {
      const [, local, , remote] = line.split(/\s+/);
      if (/^0+$/.test(local)) continue;
      const revision = /^0+$/.test(remote) ? local : `${remote}..${local}`;
      const remoteName = process.argv[process.argv.indexOf('--remote') + 1];
      const knownRemote = process.argv.includes('--remote') && /^[A-Za-z0-9_.-]+$/.test(remoteName || '') && !remoteName.startsWith('-') && git('remote').trim().split('\n').includes(remoteName);
      // A new branch shares already-published ancestors. Scan every new commit,
      // including files later deleted, without treating remote history as a new upload.
      const excluded = /^0+$/.test(remote) && knownRemote ? ['--not', `--remotes=${remoteName}`] : [];
      for (const commit of git('rev-list', revision, ...excluded).trim().split('\n').filter(Boolean)) check(commit);
    }
  }
} catch (error) { console.error(error.message); process.exit(1); }
