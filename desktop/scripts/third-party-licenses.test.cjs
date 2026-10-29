const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {verify} = require('./verify-third-party-licenses.cjs');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-license-test-'));
function put(name, text) {const p=path.join(root,name);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,text);return crypto.createHash('sha256').update(text).digest('hex')}
const notices='resources/third-party-licenses/';
const electron=put('LICENSE.electron.txt','electron notice');
const chromium=put('LICENSES.chromium.html','chromium notices');
put(notices+'runtime/LICENSE.electron.txt','electron notice');put(notices+'runtime/LICENSES.chromium.html','chromium notices');
const binary=put('resources/service/service.exe','fixture binary');
const manifest={schema_version:1,entries:[{}],files:{'runtime/LICENSE.electron.txt':electron,'runtime/LICENSES.chromium.html':chromium},service_files:{'service.exe':binary},commercial_clearance:false,unresolved:['needs source review']};
function save(){put(notices+'manifest.json',JSON.stringify(manifest))}
save();assert.equal(verify(root,{requireClearance:false}).commercialClearance,false);
assert.throws(()=>verify(root),/review is incomplete/);
put(notices+'runtime/LICENSE.electron.txt','tampered');assert.throws(()=>verify(root,{requireClearance:false}),/License file missing or changed/);
put(notices+'runtime/LICENSE.electron.txt','electron notice');
put('resources/service/unreviewed.dll','extra');assert.throws(()=>verify(root,{requireClearance:false}),/file set differs/);fs.unlinkSync(path.join(root,'resources/service/unreviewed.dll'));
put('resources/service/service.exe','new build');assert.throws(()=>verify(root,{requireClearance:false}),/differs from audited binary/);put('resources/service/service.exe','fixture binary');
manifest.commercial_clearance=true;manifest.unresolved=[];save();assert.equal(verify(root).commercialClearance,true);
console.log('PASS: intact collection, unresolved release block, tampered notice, extra binary, changed binary, cleared fixture');
// Remove only the exact temporary directory created by this process.
if(!path.resolve(root).startsWith(path.resolve(os.tmpdir())+path.sep)||!path.basename(root).startsWith('noteford-license-test-'))throw Error('Unsafe fixture path');
fs.rmSync(root,{recursive:true});
