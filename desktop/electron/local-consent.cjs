'use strict';

// Native approval is owned by the main process, never by an MCP client or renderer.
function createLocalConsent({ pairing, connections, listLibraries, confirm, onError }) {
  let running = false;
  let again = false;
  return async function refresh() {
    again = true;
    if (running) return;
    running = true;
    try {
      while (again) {
        again = false;
        for (const item of await pairing.requests()) {
          let request;
          let createdGrant;
          try {
            request = await pairing.review(item.request_id);
            const libraries = await listLibraries();
            // Capture the account BEFORE asking; switching accounts cannot redirect approval.
            const library = request.grant_client
              ? libraries.find(value => value.id === request.library_id)
              : libraries.find(value => value.is_current);
            if (!library) throw new Error('请先添加并选择账号，再重新连接 AI');
            if (!await confirm(request, library)) {
              await pairing.deny(request.request_id);
              continue;
            }
            let grant = request.grant_client;
            if (!grant) {
              createdGrant = connections.create({ label: request.client_name, library_id: library.id, scope: 'all_indexed', allow_sync: true }).id;
              grant = createdGrant;
            }
            await pairing.approve(request.request_id, grant);
          } catch (error) {
            if (createdGrant) connections.revoke(createdGrant);
            if (request) await pairing.deny(request.request_id).catch(() => undefined);
            onError(error);
          }
        }
      }
    } finally { running = false; }
  };
}
module.exports = { createLocalConsent };
