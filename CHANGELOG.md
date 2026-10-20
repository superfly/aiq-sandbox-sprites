# Changelog

## 0.1.1 - 2026-10-20

- Check configured packages when attaching to an existing Sprite and complete
  unfinished setup while preserving already-installed packages.
- Use the image's trusted toolchain path for bootstrap commands.
- Retry transient Sprite deletion failures during cleanup and always release
  the API client.
- Warn when failed setup discards an attached Sprite's earlier workspace.
- Pin CI and release checks to the supported AI-Q revision and document that
  the renamed Deep Researcher Agent interface is not yet supported.
