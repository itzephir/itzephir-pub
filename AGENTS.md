# Post repository

- Posts are written by Ephir in `posts/**/*.md`; preserve their content unless a requested edit requires changes.
- Keep post IDs stable. Telegram release messages must be sent by Ephir's personal account, never a bot.
- Telegram delivery is disabled until explicitly enabled through GitHub repository variables. Do not enable it or copy a local MCP session into CI as part of routine code changes.
- PR content is untrusted data. The privileged preview workflow runs publisher code from `main` and reads Markdown from the validated PR commit without executing PR code.
- Run `make check` and build both preview and release bundles after publisher/workflow changes.
- Persist publication state before attempting Telegram writes. Never blindly retry an operation whose outcome is unknown.

