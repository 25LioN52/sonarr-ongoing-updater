# Security

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately on the repository's [Security tab](https://github.com/25LioN52/sonarr-ongoing-updater/security/advisories/new) instead.

Fixes go into the latest version only, published as the `latest` image tag.

## Keeping your credentials safe

The service holds a Sonarr API key, a qBittorrent WebUI password and a Telegram bot token.

- Keep them in the compose file on the server, or in a `.env` file next to it. Never commit them. `.env` is already in `.gitignore`.
- Before attaching a log or a compose file to an issue, remove the keys, passwords, tokens and private tracker links from it.
- If a key or token leaks, regenerate it in Sonarr (Settings → General → API Key) or through @BotFather, then update Seerr and this service.
