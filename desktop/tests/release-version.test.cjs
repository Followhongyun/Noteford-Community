'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {check} = require('../scripts/check-release-version.cjs');
test('promotion freeze blocks coordinated version bumps and packaging overrides', () => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'noteford-version-'));
  function write(name,value) { const p=path.join(root,name);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,value); }
  function versions(version) {
    write('desktop/package.json',JSON.stringify({version}));
    write('desktop/package-lock.json',JSON.stringify({version,packages:{'':{version}}}));
    write('pyproject.toml',`version = "${version}"\n`);
    write('src/noteford/__init__.py',`__version__ = "${version}"\n`);
  }
  try {
    write('desktop/release-policy.json',JSON.stringify({version:'0.1.1',frozen:true}));
    versions('0.1.1');assert.equal(check(root,'0.1.1'),'0.1.1');
    assert.throws(()=>check(root,'0.1.2'),/版本必须一致/);
    versions('0.1.2');assert.throws(()=>check(root),/冻结/);
    write('desktop/release-policy.json',JSON.stringify({version:'0.1.1',frozen:false}));
    assert.equal(check(root),'0.1.2');
    write('src/noteford/__init__.py','__version__ = "0.1.1"\n');assert.throws(()=>check(root),/版本必须一致/);
  } finally { fs.rmSync(root,{recursive:true,force:true}); }
});
