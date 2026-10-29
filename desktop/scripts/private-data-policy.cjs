const fs = require('node:fs');
const path = require('node:path');
function check(root) {
  root = fs.realpathSync(root);
  const privateRoots = ['Data','secrets','Archive'].map(name=>path.resolve(root,name));
  const seen = new Set();
  function walk(file) {
    if (!fs.existsSync(file)) return;
    const real = fs.realpathSync(file);
    if (privateRoots.some(p=>real===p||real.startsWith(p+path.sep))) throw Error('打包输入引用了私有数据目录');
    if (seen.has(real)) return; seen.add(real);
    if (fs.statSync(file).isDirectory()) for (const name of fs.readdirSync(file)) walk(path.join(file,name));
  }
  for (const name of ['desktop/electron','desktop/mcp-plugin','desktop/dist','desktop/assets','desktop/licenses','src/noteford','build/service','build/third-party-licenses']) walk(path.join(root,name));
}
module.exports = {check};
