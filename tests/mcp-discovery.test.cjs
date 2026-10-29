const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {publishDiscovery} = require('../desktop/electron/mcp-discovery.cjs');
test('old instance cleanup cannot remove a newer discovery record', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-discovery-'));
  try {
    const old = publishDiscovery({directory, pid: 1, alive: () => false});
    const current = publishDiscovery({directory, pid: 2, alive: () => false});
    old();
    assert.equal(JSON.parse(fs.readFileSync(path.join(directory, 'connection.json'))).pid, 2);
    current();
    assert.equal(fs.existsSync(path.join(directory, 'connection.json')), false);
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});


test('discovery repairs deleted and stale records without replacing another live owner', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-discovery-'));
  const file = path.join(directory, 'connection.json');
  const cleanup = publishDiscovery({directory, pid: 100, alive: pid => pid === 200});
  try {
    fs.unlinkSync(file); cleanup.refresh();
    assert.equal(JSON.parse(fs.readFileSync(file)).pid, 100);
    fs.writeFileSync(file, JSON.stringify({instance:'dead',pid:300})); cleanup.refresh();
    assert.equal(JSON.parse(fs.readFileSync(file)).pid, 100);
    fs.writeFileSync(file, JSON.stringify({instance:'live',pid:200})); cleanup.refresh();
    assert.equal(JSON.parse(fs.readFileSync(file)).pid, 200);
    cleanup();
    assert.equal(JSON.parse(fs.readFileSync(file)).pid, 200);
    fs.unlinkSync(file); cleanup.refresh();
    assert(!fs.existsSync(file));
  } finally { cleanup(); fs.rmSync(directory,{recursive:true,force:true}); }
});


test('running owner automatically recreates missing discovery on its timer', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-discovery-'));
  const file = path.join(directory, 'connection.json');
  const cleanup = publishDiscovery({directory, pid:100, alive:()=>false, interval:10});
  try {
    fs.unlinkSync(file);
    for(let n=0;n<50&&!fs.existsSync(file);n++) await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(JSON.parse(fs.readFileSync(file)).pid,100);
  } finally { cleanup(); fs.rmSync(directory,{recursive:true,force:true}); }
});

test('Windows reused PID belongs to another executable and cannot block discovery', {skip:process.platform !== 'win32'}, () => {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'noteford-pid-reuse-'));
  const file=path.join(directory,'connection.json');
  fs.writeFileSync(file,JSON.stringify({instance:'stale',pid:process.pid,appExecutable:'C:\\nonexistent\\Noteford.exe'}));
  const cleanup=publishDiscovery({directory,pid:process.pid,appExecutable:process.execPath});
  try {assert.notEqual(JSON.parse(fs.readFileSync(file)).instance,'stale');}
  finally {cleanup();fs.rmSync(directory,{recursive:true,force:true});}
});
