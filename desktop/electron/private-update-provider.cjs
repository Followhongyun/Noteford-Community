'use strict';
const { GenericProvider } = require('electron-updater/out/providers/GenericProvider');
const artifactMetadata = new WeakMap();
const feedUrl = 'https://followhongyun.com/updates/noteford/macos/';

class PrivateUpdateProvider extends GenericProvider {
  constructor(configuration, updater, runtimeOptions) {
    super(configuration, updater, {...runtimeOptions, isUseMultipleRangeRequest:false});
    this.privateUpdater = updater;
  }
  resolveFiles(info) {
    const files = super.resolveFiles(info);
    const base = this.baseUrl;
    for (const file of files) {
      if (file.url.origin !== base.origin || !file.url.pathname.startsWith(base.pathname)
          || file.url.username || file.url.password || file.url.search || file.url.hash) {
        throw Error('Untrusted update artifact');
      }
    }
    artifactMetadata.set(this.privateUpdater,new Map(files.map(file=>[file.url.href,file.info])));
    return files;
  }
}
function configurePrivateUpdates(updater) {
  updater.setFeedURL({ provider: 'custom', updateProvider: PrivateUpdateProvider,
    url: feedUrl, useMultipleRangeRequest: false });
  // No raw upstream URLs/errors (possibly including credentials) in normal logs.
  updater.logger = { info() {}, warn() {}, error() {}, debug() {} };
  if (typeof updater.httpExecutor.download === 'function') {
    const download=updater.httpExecutor.download.bind(updater.httpExecutor);
    updater.httpExecutor.download=(url,destination,options)=>{
      if (!url.pathname.endsWith('.zip')) return download(url,destination,options);
      const info=artifactMetadata.get(updater)?.get(url.href);
      if (!info || info.sha512!==options.sha512) throw Error('Untrusted update metadata');
      return require('./resumable-download.cjs').resumableDownload(updater.httpExecutor,url,destination,options,info.size);
    };
  }
  // Manifest and ZIP requests must stay on the protected origin, including
  // redirects and differential download requests.
  const request = updater.httpExecutor.createRequest.bind(updater.httpExecutor);
  updater.httpExecutor.createRequest = (options, callback) => {
    if (options.protocol !== 'https:' || options.hostname !== 'followhongyun.com'
        || (options.port && String(options.port) !== '443')
        || !options.path?.startsWith('/updates/noteford/macos/')) {
      throw Error('Untrusted update request');
    }
    return request(options, callback);
  };
}
module.exports = { configurePrivateUpdates, PrivateUpdateProvider, feedUrl };
