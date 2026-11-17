# Contributing

All changes arrive as pull requests, including the maintainer's own.

## Adding an app

1. Create `apps/<id>/` with `app.yaml` (and `compose.yaml` or inlined compose, icon, README).
2. Folder name must equal the app `id`.
3. Follow the manifest schema in `schema/app.schema.json`.
4. Run `python scripts/build-index.py --check` locally.
5. Open a pull request.

## Review checklist

- Image comes from the official or well-known publisher.
- Tag or digest is pinned (never `latest`).
- No privileged mode or host networking unless justified.
- Volumes mount only what the app needs.
- No secrets or hardcoded credentials.
- Ports and defaults are sensible.
- QEMU images include a SHA-256.
- Icon is PNG or SVG and under 256 KB.