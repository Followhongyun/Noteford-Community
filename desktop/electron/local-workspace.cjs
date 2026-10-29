'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { AsyncLocalStorage } = require('node:async_hooks');
const { execFileSync } = require('node:child_process');
function processStartedAtMs(){
  if(process.platform==='darwin'){
    const stamp=execFileSync('/bin/ps',['-p',String(process.pid),'-o','lstart='],{encoding:'utf8',env:{...process.env,LC_ALL:'C'}}).trim();
    const value=Date.parse(stamp);if(!Number.isFinite(value))throw Error('Cannot verify desktop process start');return value;
  }
  if(process.platform==='win32'){
    const value=Number(execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',`([DateTimeOffset](Get-Process -Id ${process.pid}).StartTime).ToUnixTimeMilliseconds()`],{encoding:'utf8',windowsHide:true}).trim());
    if(!Number.isFinite(value)||value<=0)throw Error('Cannot verify desktop process start');return value;
  }
  return Date.now()-process.uptime()*1000;
}

function atomicJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = file + '.' + randomUUID() + '.tmp';
  try {
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { flag: 'wx', mode: 0o600 });
    fs.renameSync(tmp, file);
  } finally { try { fs.unlinkSync(tmp); } catch {} }
}

class LocalWorkspace {
  constructor(installationRoot) {
    fs.mkdirSync(installationRoot, {recursive:true,mode:0o700});
    this.installation=fs.realpathSync(installationRoot);
    this.leaseFile=path.join(this.installation,'data/runtime/community/active-workspace.json');
    this.context=new AsyncLocalStorage(); this.active=null;
    if(fs.existsSync(path.join(this.installation,'.product-workspace.json'))) throw Error('Refusing member-version data directory');
    this.lock();
  }
  lock(){this.active?.abort.abort();this.active=null;atomicJson(this.leaseFile,{version:1,active:false});}
  assert(scope=this.context.getStore()||this.active){
    if(!scope||scope!==this.active||scope.abort.signal.aborted)throw Error('本地工作区已关闭，请重新打开应用。');
    return scope;
  }
  activate(){
    if(this.active)throw Error('workspace_busy');
    const root=this.installation,workspace='community';
    const configFile=path.join(root,'config.local.json');
    if(!fs.existsSync(configFile))atomicJson(configFile,{project_root:root,wechat_roots:[],library_registry_path:path.join(root,'data/library_registry.json'),allow_memory_rescan:true,auto_refresh_interval_seconds:300});
    const config=JSON.parse(fs.readFileSync(configFile,'utf8'));
    if(fs.realpathSync(path.resolve(root,config.project_root||'.'))!==root)throw Error('storage');
    atomicJson(path.join(root,'.community-workspace.json'),{version:1,installation:root,workspace});
    const scope={root,workspace,epoch:randomUUID(),abort:new AbortController()};
    atomicJson(this.leaseFile,{version:1,active:true,root,workspace,epoch:scope.epoch,pid:process.pid,appExecutable:process.execPath,processStartedAtMs:processStartedAtMs()});
    this.active=scope;return scope;
  }
  run(scope,callback){this.assert(scope);return this.context.run(scope,callback);}
  guard(scope,object){return new Proxy(object,{get:(target,key)=>typeof target[key]==='function'?(...args)=>{this.assert(scope);return target[key](...args)}:target[key]});}
}
module.exports={LocalWorkspace,atomicJson};
