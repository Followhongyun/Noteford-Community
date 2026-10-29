'use strict';

const { spawn } = require('child_process');

function registerMacWechat(ipcMain, options) {
  ipcMain.handle('noteford:mac-wechat', (_event, input = {}) => runMacEntry(options, input));
}

function runMacEntry(options, input) {
  options.assertActive?.();
  if (options.scope) {
    if (['state', 'project', 'hook'].some(key => input[key])) throw Error('managed_workspace_paths_only');
    // Capture and import use the authenticated HTTP routes. This legacy CLI only probes status.
    if (input.action && input.action !== 'status') throw Error('managed_workspace_action_denied');
  }
  const action = String(input.action || 'status');
  const args = ['-m', 'noteford.mac_entry', action];
  if (input.app) args.push('--app', String(input.app));
  if (input.hook) args.push('--hook', String(input.hook));
  if (input.state) args.push('--state', String(input.state));
  if (input.dbRoot) args.push('--db-root', String(input.dbRoot));
  if (input.project) args.push('--project', String(input.project));
  if (input.imageDat) args.push('--image-dat', String(input.imageDat));
  if (input.noRelaunch) args.push('--no-relaunch');
  if (action === 'serve') args.push('--token-file', require('path').join(options.runtimeRoot, 'secrets', 'api_token.json'));
  return new Promise((resolve, reject) => {
    const child = spawn(options.python, args, {
      env: {
        ...process.env,
        PYTHONPATH: [options.src, process.env.PYTHONPATH].filter(Boolean).join(require('path').delimiter),
        ...(options.scope ? { NOTEFORD_WORKSPACE_ROOT: options.scope.root, NOTEFORD_WORKSPACE_EPOCH: options.scope.epoch } : {}),
        PYTHONUTF8: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    child.on('error', reject);
    child.on('exit', code => {
      try {
        const payload = JSON.parse(stdout);
        payload.exit_code = code;
        resolve(payload);
      } catch {
        reject(new Error(stderr || stdout || `mac_entry exited ${code}`));
      }
    });
  });
}

module.exports = { registerMacWechat, runMacEntry };
