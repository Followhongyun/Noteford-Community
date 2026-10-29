'use strict';

const {Worker, isMainThread, parentPort} = require('node:worker_threads');

function validate(input) {
  if (!input || !['width', 'height', 'radius', 'scale', 'stroke', 'corners'].every(k => Number.isFinite(input[k]))) throw Error('Invalid corner geometry');
  const {width, height, radius, scale, stroke, corners} = input;
  if (width <= 0 || height <= 0 || width > 8192 || height > 8192 || radius < 0 || radius > Math.max(width, height)
    || scale < 1 || scale > 4 || stroke < 0 || stroke > Math.min(width, height) / 2
    || corners !== 15
    || Math.ceil(width * scale) * Math.ceil(height * scale) > 8_000_000) throw Error('Corner geometry out of bounds');
  return {width, height, radius, scale, stroke, corners};
}

if (!isMainThread) {
  const render = require('./mac-corner-renderer.cjs').createRenderer();
  parentPort.on('message', ({id, input}) => {
    try {
      const value = validate(input);
      const outer = render({...value, stroke: 0});
      const stroke = value.stroke ? render(value) : null;
      parentPort.postMessage({id, result: {outer, stroke, renderer: 'macos-continuous'}});
    } catch { parentPort.postMessage({id, result: null}); }
  });
}

// Geometry work stays off Electron's UI thread. Requests/cache are bounded and
// deduplicated; a missing native library degrades to the renderer's G3 fallback.
function createNativeCorners() {
  let worker, sequence = 0, unavailable = false;
  const pending = new Map(), cache = new Map();
  function stop() {
    unavailable = true;
    void worker?.terminate(); worker = null;
    for (const request of pending.values()) { clearTimeout(request.timer); request.resolve(null); }
    pending.clear(); cache.clear();
  }
  return {
    render(raw) {
      let input;
      try { input = validate(raw); } catch { return Promise.resolve(null); }
      if (unavailable || process.platform !== 'darwin') return Promise.resolve(null);
      const key = JSON.stringify(input);
      if (cache.has(key)) return cache.get(key);
      if (pending.size >= 64) return Promise.resolve(null);
      if (!worker) {
        worker = new Worker(__filename);
        worker.on('message', ({id, result}) => {
          const request = pending.get(id);
          if (request) { clearTimeout(request.timer); pending.delete(id); request.resolve(result); }
        });
        worker.on('error', stop);
        worker.on('exit', stop);
        worker.unref();
      }
      const id = ++sequence;
      const promise = new Promise(resolve => {
        pending.set(id, {resolve, timer: setTimeout(stop, 5000)});
        worker.postMessage({id, input});
      });
      cache.set(key, promise);
      if (cache.size > 96) cache.delete(cache.keys().next().value);
      return promise;
    },
    close: stop,
  };
}

module.exports = {validate, createNativeCorners};
