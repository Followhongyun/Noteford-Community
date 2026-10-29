# Noteford Community

[简体中文](README.md) · **English**

Turn locally available WeChat data into a library you can read, search, and use with your AI assistant.

[Download for Mac · 0.1.1](https://github.com/Followhongyun/Noteford-Community/releases/latest) · [Installation & AI setup (Chinese)](docs/INSTALL.md) · [Release notes (Chinese)](CHANGELOG.md)

## What it does

- **Read and search** followed conversations, find messages, and open their original context.
- **Sync your library** manually, on a schedule, or through an authorized AI request. Each sync that actually starts resets the next scheduled sync time.
- **Browse media and materials** including cached images, videos, and attachments. Some originals may have expired or be unavailable.
- **Collapse chat noise** such as standalone stickers, animations, recall notices, and “pat” notifications into expandable counts.
- **Connect through Skill and MCP** so a local AI client can search authorized data and cite original messages.
- **Update through GitHub** by downloading a newer stable version and installing it when the app quits normally.

The community edition does not include the Discovery homepage or require a Hongyun product account. It is maintained independently from the member edition; future member features are not guaranteed to be included.

## Download and get started

| Item | Current support |
| --- | --- |
| Version | **0.1.1** |
| Hardware | Apple Silicon Mac (M1 or later) |
| System | macOS 13 or later |
| Installer | `Noteford-0.1.1-macOS.dmg`, Developer ID signed and Apple notarized |
| Intel / Windows | No validated installer currently available |

1. Download the DMG from the [latest release](https://github.com/Followhongyun/Noteford-Community/releases/latest), open it, and drag **Noteford 开源版** into **Applications**.
2. Launch it from Applications, connect your own WeChat account, and approve the requested system permissions.
3. Choose conversations to follow. Once the first sync finishes, start reading, searching, and browsing materials.

The app bundles its runtime; Node.js and Python are not required to run it. Data is stored in `~/Noteford Community/Data`. It can coexist with the member development edition, using separate accounts, data, and authorizations.

**Already using an early community release?** Quit the app, then install 0.1.1 manually over the existing application without deleting its data directory. Superseded pre-promotion downloads have been removed. This is a version-number migration, not a return to older functionality; automatic downgrades are disabled.

## Connect your AI assistant

Keep Noteford Community running and send this to an AI client that can execute local commands:

> Install and connect Noteford Community: `npx skills add Followhongyun/Noteford-Community --skill noteford-community`. Follow the Skill to register MCP for this client, complete first-use authorization, and verify the connection by calling list_libraries and get_status.

Installing the Skill requires Node.js/npm; the Codex registration script also requires Python 3. Installing files does not grant access to chats. You must personally approve the first read request in Noteford.

The Codex MCP server is named `noteford-community`. If the current conversation has not loaded the new tools, start a new conversation. Other clients use their own documented MCP configuration. The local stdio interface does not work with cloud-only clients. See the [setup and troubleshooting guide (Chinese)](docs/INSTALL.md).

## Updates and contact

Use the app's update section in Settings to check GitHub. Newer stable versions download automatically and install on normal quit. The community app never uses the member update channel.

The version is currently frozen at **0.1.1** until the maintainer explicitly confirms that the freeze can be lifted. Revisions with the same version number do not trigger automatic updates. See the [version policy (Chinese)](docs/DEVELOPMENT.md#版本号规范2026-10-29-起).

For future updates, contact Hongyun: hover over the QR-code icon at the bottom of the app's Settings page to view the WeChat contact card.

## Build from source

Use Node.js 24 (minimum 22.19), Python 3.11, and the Mac build tools:

```sh
node desktop/scripts/install-dependencies.cjs
npm --prefix desktop run build
npm --prefix desktop run package:mac -- --dir
```

Independent builds require your own signing configuration; the maintainer's certificate is not included. Development entry points, tests, and build limitations are documented in the [development guide (Chinese)](docs/DEVELOPMENT.md).

## Documentation and license

The detailed documentation is in Chinese: [Features](docs/PRODUCT.md) · [Architecture](docs/ARCHITECTURE.md) · [Data & permissions](docs/DATA-AND-SECURITY.md) · [Validation records](docs/STATUS.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md).

Original source code is licensed under [AGPL-3.0-only](LICENSE). Third-party components retain their own licenses and [notices](NOTICE.md). Commercial use and redistribution follow the applicable licenses; membership services do not add restrictions to rights granted by the open-source license.

Noteford is not affiliated with Tencent. Use it only with data you are authorized to access. It does not send WeChat messages; compatibility and availability vary across WeChat versions, media formats, and historical resources.
