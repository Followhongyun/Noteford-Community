'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {createHash,randomUUID}=require('node:crypto');
const {execFileSync}=require('node:child_process');
function sourceLayout(desktop,platform=process.platform,home=os.homedir()){
  const project=path.dirname(desktop);
  const cache=path.join(home,'Library','Caches','Noteford Community','Source',createHash('sha256').update(project).digest('hex').slice(0,12));
  return {project,data:path.join(project,'Data'),cache,python:path.join(project,'.venv-build',platform==='win32'?'Scripts/python.exe':'bin/python3')};
}
/** A source-only Electron runtime. Never modifies /Applications or stored credentials. */
function prepareSourceRuntime(desktop,cache){
  if(process.platform!=='darwin')return require(path.join(desktop,'node_modules','electron'));
  const original=path.join(desktop,'node_modules/electron/dist/Electron.app');
  const policy=require(path.join(desktop,'package.json'));
  const signer=require('./mac-pack.cjs');
  const identity=signer.resolveDeveloperIdentity({teamId:policy.notefordMacSigningTeamId});
  const digest=createHash('sha256').update('noteford-source-runtime-v2').update(identity)
    .update(fs.readFileSync(path.join(original,'Contents/MacOS/Electron')))
    .update(fs.readFileSync(path.join(original,'Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework')))
    .update(fs.readFileSync(path.join(desktop,'assets/noteford.icns'))).digest('hex').slice(0,20);
  const directory=path.join(cache,'runtime',digest),app=path.join(directory,'Electron.app'),binary=path.join(app,'Contents/MacOS/Electron');
  if(fs.existsSync(path.join(directory,'ready.json'))&&fs.existsSync(binary)){
    execFileSync('/usr/bin/codesign',['--verify','--deep','--strict',app],{stdio:'pipe'});return binary;
  }
  fs.mkdirSync(path.dirname(directory),{recursive:true});
  const staging=directory+'.preparing-'+randomUUID(),target=path.join(staging,'Electron.app');
  fs.cpSync(original,target,{recursive:true,verbatimSymlinks:true});
  const info=path.join(target,'Contents/Info.plist');
  // Chromium safeStorage derives its macOS keychain service from CFBundleName.
  // Keep the executable named Electron so this remains an unpackaged source runtime.
  execFileSync('/usr/libexec/PlistBuddy',['-c','Set :CFBundleIdentifier com.followhongyun.noteford.community',info]);
  execFileSync('/usr/libexec/PlistBuddy',['-c','Set :CFBundleDisplayName Noteford 开源版',info]);
  execFileSync('/usr/libexec/PlistBuddy',['-c','Set :CFBundleName Noteford Community',info]);
  fs.copyFileSync(path.join(desktop,'assets/noteford.icns'),path.join(target,'Contents/Resources/electron.icns'));
  signer.signMacApp(target,{teamId:policy.notefordMacSigningTeamId,env:{...process.env,CSC_NAME:identity}});
  fs.writeFileSync(path.join(staging,'ready.json'),JSON.stringify({source:original,identifier:'com.followhongyun.noteford.community'}));
  fs.renameSync(staging,directory);return binary;
}
function prepareSourcePython(desktop,cache,python){
  if(process.platform!=='darwin')return python;
  const policy=require(path.join(desktop,'package.json'));
  const identity=require('./mac-pack.cjs').resolveDeveloperIdentity({teamId:policy.notefordMacSigningTeamId});
  const resolved=fs.realpathSync(python),venv=path.resolve(path.dirname(python),'..');
  // Homebrew's bin/python launcher re-execs Python.app, losing the launcher's identity.
  const frameworkBinary=path.resolve(path.dirname(resolved),'../Resources/Python.app/Contents/MacOS/Python');
  const source=fs.existsSync(frameworkBinary)?frameworkBinary:resolved;
  const config=fs.readFileSync(path.join(venv,'pyvenv.cfg'));
  const hash=createHash('sha256').update('noteford-source-python-v2').update(identity).update(source).update(fs.readFileSync(source)).update(config).digest('hex').slice(0,20);
  const directory=path.join(cache,'python',hash),binary=path.join(directory,'bin/python3');
  if(fs.existsSync(binary)){execFileSync('/usr/bin/codesign',['--verify','--strict',binary],{stdio:'pipe'});return binary;}
  const staging=directory+'.preparing-'+randomUUID();
  fs.mkdirSync(path.join(staging,'bin'),{recursive:true});
  fs.copyFileSync(source,path.join(staging,'bin/python3'));
  fs.writeFileSync(path.join(staging,'pyvenv.cfg'),config);
  fs.symlinkSync(path.join(venv,'lib'),path.join(staging,'lib'),'dir');
  // Same designated identity as the released service; no copying or exporting secrets.
  execFileSync('/usr/bin/codesign',['--force','--sign',identity,'--identifier','noteford-community-service','--options','runtime','--timestamp','--entitlements',path.join(desktop,'entitlements.mac.plist'),path.join(staging,'bin/python3')],{stdio:'pipe'});
  execFileSync('/usr/bin/codesign',['--verify','--strict',path.join(staging,'bin/python3')],{stdio:'pipe'});
  fs.renameSync(staging,directory);return binary;
}
module.exports={sourceLayout,prepareSourceRuntime,prepareSourcePython};
