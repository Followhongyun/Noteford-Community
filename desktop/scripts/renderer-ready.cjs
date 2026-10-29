'use strict';
const { stripVTControlCharacters } = require('node:util');

// Only accept the URL announced by this strict-port child, never a port probe
// that could belong to another application. Keep raw chunks until normalizing,
// since even the ANSI escape sequence itself can span output chunks.
function waitForRenderer(child, { timeoutMs = 20000, write = () => {} } = {}) {
  return new Promise((resolve, reject) => {
    let stdout = '', stderr = '', settled = false;
    const finish = error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener('error', onError);
      child.removeListener('exit', onExit);
      error ? reject(error) : resolve();
    };
    const onError = error => finish(error);
    const onExit = (code, signal) => finish(new Error(`Renderer exited before ready (code ${code}, signal ${signal}): ${stripVTControlCharacters(stderr).trim()}`));
    const timer = setTimeout(() => finish(new Error(`Renderer startup timed out: ${stripVTControlCharacters(stderr).trim()}`)), timeoutMs);
    child.stdout.on('data', chunk => {
      write(chunk);
      stdout = (stdout + chunk.toString()).slice(-16000);
      if (/http:\/\/127\.0\.0\.1:5173\//.test(stripVTControlCharacters(stdout)) && child.exitCode === null && !child.killed) finish();
    });
    child.stderr.on('data', chunk => { write(chunk); stderr = (stderr + chunk.toString()).slice(-4000); });
    child.once('error', onError);
    child.once('exit', onExit);
  });
}
module.exports = { waitForRenderer };
