const {execFileSync} = require('node:child_process');
const path = require('node:path');

function packWindows() {
  require('./node-trust.cjs');
  require('./prepare-installer-template.cjs')();
  const root = path.resolve(__dirname, '../..');
  execFileSync(path.join(root, '.venv-build', 'Scripts', 'python.exe'),
    [path.join(root, 'Scripts', 'prepare-license-sources.py')],
    {cwd: root, windowsHide: true, stdio: 'inherit'});
  execFileSync(path.join(root, '.venv-build', 'Scripts', 'python.exe'),
    [path.join(root, 'Scripts', 'collect-third-party-licenses.py')],
    {cwd: root, windowsHide: true, stdio: 'inherit'});
}

module.exports = async context => {
  require('./check-release-version.cjs').check(undefined, context?.packager?.appInfo?.version);
  require('./private-data-policy.cjs').check(path.resolve(__dirname, '../..'));
  if (process.platform === 'darwin') {
    require('./mac-pack.cjs').prepareMacPack(path.resolve(__dirname, '../..'));
    return;
  }
  packWindows();
};
module.exports.packWindows = packWindows;
