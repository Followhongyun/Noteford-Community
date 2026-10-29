const { safeStorage, app, net } = require("electron");
const { ProductAccount } = require("./product-account.cjs");
function registerProductAccount({
  ipcMain,
  assertMainRenderer,
  getWindow,
  directory,
  activate,
  deactivate,
  changed = () => {},
}) {
  const config = require("./product-account-config.json");
  const service = new ProductAccount({
    directory,
    safeStorage,
    activate,
    deactivate,
    // Chromium uses the same system proxy/network configuration as the desktop.
    fetcher: (url, options) => net.fetch(url, options),
    appVersion: app.getVersion(),
    development: !app.isPackaged,
    apiOrigin:
      (!app.isPackaged && process.env.NOTEFORD_ACCOUNTS_API_ORIGIN) ||
      config.apiOrigin,
    changed: (state) => {
      getWindow()?.webContents.send("noteford:product-account-state", state);
      changed(state);
    },
  });
  const actions = {
    status: () => service.snapshot(),
    restore: () => service.restore(),
    submit: (input) => service.submit(input),
    code: (input) => service.sendCode(input?.email, input?.purpose),
    cancel: () => service.cancel(),
    logout: () => service.logout(),
    local: () => service.local(),
  };
  for (const [name, run] of Object.entries(actions))
    ipcMain.handle("noteford:product-account-" + name, (event, input) => {
      assertMainRenderer(event);
      return run(input);
    });
  return service;
}
module.exports = { registerProductAccount };
