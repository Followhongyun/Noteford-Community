'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
async function saveMedia(input, {mediaMap, isAllowedLocalMediaPath, showSaveDialog}) {
  const url = new URL(String(input.url || ''));
  const candidate = mediaMap.get(url.pathname.slice(1));
  if (url.protocol !== 'noteford-media:' || url.hostname !== 'asset' || !candidate) throw new Error('Blocked media path');
  const source = await fs.realpath(candidate);
  if (!isAllowedLocalMediaPath(source) || !(await fs.stat(source)).isFile()) throw new Error('Blocked media path');
  const name = path.basename(String(input.name || path.basename(source))).replace(/[\x00-\x1f:]/g, '_');
  const ext = path.extname(source);
  const defaultPath = name && name !== '.' && name !== '..' ? name + (path.extname(name) ? '' : ext) : path.basename(source);
  const result = await showSaveDialog({title: '另存为', defaultPath, properties: ['createDirectory', 'showOverwriteConfirmation']});
  if (result.canceled || !result.filePath) return false;
  // Revalidate after the user has chosen a destination.
  if (await fs.realpath(candidate) !== source || !isAllowedLocalMediaPath(source)) throw new Error('Blocked media path');
  if (path.resolve(result.filePath) !== source) await fs.copyFile(source, result.filePath);
  return true;
}
module.exports = {saveMedia};
