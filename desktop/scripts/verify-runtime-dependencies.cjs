'use strict';

// Inspect the packaged tree, not the developer's node_modules: shared workspace
// symlinks can make the packager omit transitive production dependencies.
module.exports = function verifyRuntimeDependencies(archive) {
  const {extractFile, listPackage} = require('@electron/asar');
  const files = new Set(listPackage(archive).map(name => name.replace(/^[/\\]/, '').replaceAll('\\', '/')));
  const visited = new Set();
  function visit(directory) {
    if (visited.has(directory)) return;
    visited.add(directory);
    const manifest = JSON.parse(extractFile(archive, `${directory ? directory + '/' : ''}package.json`));
    for (const name of Object.keys(manifest.dependencies || {})) {
      if (manifest.optionalDependencies?.[name]) continue;
      let base = directory;
      let resolved;
      while (true) {
        const candidate = `${base ? base + '/' : ''}node_modules/${name}`;
        if (files.has(`${candidate}/package.json`)) {
          resolved = candidate;
          break;
        }
        if (!base) break;
        const parent = base.lastIndexOf('/node_modules/');
        base = parent >= 0 ? base.slice(0, parent) : '';
      }
      if (!resolved) throw Error(`Packaged runtime dependency missing: ${manifest.name} -> ${name}`);
      visit(resolved);
    }
  }
  visit('');
  return {packages: visited.size};
};
