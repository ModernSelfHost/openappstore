# openappstore — Design Doc

*Status: draft v0.1 · Author: Wit*

## 1. Overview

openappstore is a curated GitHub repository of app definitions for self-hosted systems. Each entry describes everything a platform needs to install and run one app: how it runs (Docker Compose or QEMU), which values the user can configure, and the metadata needed to show it in a store UI. A generated `index.json` lets any client browse the catalog by fetching a single small file.

The repo is a **data source, not a program**. Any self-hosted OS, dashboard, or CLI can consume it.

## 2. Goals and non-goals

### Goals

- One custom manifest format that supports both **Docker Compose** and **QEMU** apps.
- A **generated JSON index** so clients browse fast and update cheaply.
- A **form-friendly `inputs` block**, so clients can render a setup screen without app-specific code.
- Every entry **reviewed by the maintainer** before it lands.
- Plain Git as the distribution mechanism: forkable, mirrorable, auditable.

### Non-goals

- Hosting container images or VM disk images (entries only reference them).
- Running or orchestrating apps. That is the consumer's job.
- A user-submitted free-for-all. Entries are curated.
- Sandboxing or signing in v1 (see Security).

## 3. Architecture

```
┌──────────────────────┐   CI: validate + build    ┌─────────────┐
│  apps/<id>/app.yaml  │ ────────────────────────▶ │  index.json │
│  icon, README        │                           └──────┬──────┘
└──────────────────────┘                                  │ HTTPS (raw / Pages)
                                                          ▼
                                              ┌────────────────────┐
                                              │ Consumer (OS, UI,  │
                                              │ CLI) browses index,│
                                              │ fetches app.yaml,  │
                                              │ renders inputs,    │
                                              │ runs compose/qemu  │
                                              └────────────────────┘
```

Three parts:

1. **Catalog**: the `apps/` directory, one folder per app.
2. **Schema**: a JSON Schema describing a valid `app.yaml`.
3. **Index builder**: a CI script that validates every manifest and emits `index.json`.

## 4. Repository layout

```
openappstore/
├── apps/
│   ├── jellyfin/
│   │   ├── app.yaml
│   │   ├── compose.yaml      # optional, if not inlined
│   │   ├── icon.png
│   │   └── README.md
│   └── haos/
│       ├── app.yaml
│       └── icon.png
├── schema/
│   └── app.schema.json
├── scripts/
│   └── build-index.*         # validate + generate
├── index.json                # generated, never hand-edited
├── CONTRIBUTING.md
└── .github/workflows/
    └── ci.yml
```

Rules:

- Folder name equals the app `id`.
- `index.json` is committed by CI (or published to Pages), never edited by hand.
- Icons are PNG or SVG with a size cap (for example 256 KB).

## 5. Manifest format (`app.yaml`)

### 5.1 Common fields

| Field | Required | Description |
| --- | --- | --- |
| `schemaVersion` | yes | Manifest format version (integer). |
| `id` | yes | Lowercase slug, unique, matches folder name. |
| `name` | yes | Display name. |
| `version` | yes | Version of the packaged app. |
| `description` | yes | One or two sentences. |
| `category` | yes | One of a fixed list (media, files, network, home, dev, ...). |
| `homepage` | no | Upstream project URL. |
| `icon` | no | Relative path to icon file. |
| `runtime` | yes | `compose` or `qemu`. |
| `inputs` | no | User-configurable values (section 5.4). |
| `requirements` | no | Minimum CPU, RAM, disk, architecture. |

### 5.2 Compose runtime

```yaml
schemaVersion: 1
id: jellyfin
name: Jellyfin
version: 10.9.11
description: Self-hosted media server.
category: media
runtime: compose
compose:
  file: compose.yaml        # or: inline: |
    # ...
inputs:
  - key: MEDIA_PATH
    label: Media folder
    type: path
    default: /mnt/media
  - key: PORT
    label: Web port
    type: port
    default: 8096
```

The compose file references inputs as `${MEDIA_PATH}` and `${PORT}`. The consumer substitutes values (via an `.env` file or the environment) before running `docker compose up`.

### 5.3 QEMU runtime

```yaml
schemaVersion: 1
id: haos
name: Home Assistant OS
version: 13.2
description: Home Assistant as a full VM appliance.
category: home
runtime: qemu
qemu:
  image:
    url: https://example.com/haos_ova-13.2.qcow2.xz
    sha256: <hash>
    format: qcow2
    compression: xz
  machine:
    arch: x86_64
    cpus: 2
    memory: 2048        # MiB
    disk: 32G           # resize target
    uefi: true
  network:
    mode: user          # user | bridge
    forward:
      - { host: 8123, guest: 8123, proto: tcp }
  devices:              # optional passthrough hints
    usb: []
inputs:
  - key: HOST_PORT
    label: Web port
    type: port
    default: 8123
    binds: qemu.network.forward[0].host
```

The QEMU block is declarative. The consumer translates it into a QEMU command line (or a libvirt definition). The manifest never contains raw command-line flags, which keeps entries portable and reviewable.

### 5.4 Inputs

Inputs let a consumer render a setup form with no per-app code.

| Field | Description |
| --- | --- |
| `key` | Variable name. Used in compose as `${KEY}`; in QEMU via `binds`. |
| `label` | Human-readable label. |
| `type` | `string`, `number`, `bool`, `port`, `path`, `password`, `select`. |
| `default` | Default value. |
| `required` | Whether the user must provide a value. |
| `options` | For `select`: allowed values. |
| `help` | Optional help text. |
| `binds` | QEMU only: manifest path this value overrides. |

`password` inputs may specify `generate: true` so the consumer creates a random value instead of asking.

## 6. The generated index

`index.json` is produced by CI and contains only what a browse screen needs:

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-11-17T00:00:00Z",
  "apps": [
    {
      "id": "jellyfin",
      "name": "Jellyfin",
      "version": "10.9.11",
      "category": "media",
      "description": "Self-hosted media server.",
      "runtime": "compose",
      "icon": "apps/jellyfin/icon.png",
      "manifest": "apps/jellyfin/app.yaml",
      "sha256": "<hash of the app folder contents>"
    }
  ]
}
```

Client flow:

1. Fetch `index.json` (one small request).
2. Show the catalog, search, and filter by category or runtime.
3. On install, fetch only that app's manifest (and compose file).
4. Compare the stored `sha256` against the index to detect updates.

The per-app hash covers the whole app folder, so changes to the compose file or icon also trigger an update.

## 7. CI pipeline

On every pull request and push to `main`:

1. **Schema validation**: every `app.yaml` against `schema/app.schema.json`.
2. **Consistency checks**: folder name equals `id`; referenced files exist; icon size within limit; every `${VAR}` used in a compose file has a matching input or is explicitly allowed.
3. **Compose lint**: `docker compose config` to confirm the file parses.
4. **Pinning check**: images must use a tag or digest, never `latest` (configurable allowlist).
5. **Reachability check**: QEMU image URLs respond, and where feasible the checksum matches.
6. **Index build**: generate `index.json`; on `main`, commit it or publish it to GitHub Pages.

## 8. Review and contribution flow

- All changes arrive as pull requests, including the maintainer's own.
- The maintainer reviews every entry before merge. CI is a safety net, not a replacement.
- `CONTRIBUTING.md` carries a review checklist:
  - Image comes from the official or well-known publisher.
  - Tag or digest is pinned.
  - No privileged mode or host networking unless justified.
  - Volumes mount only what the app needs.
  - No secrets or hardcoded credentials.
  - Ports and defaults are sensible.
- Optional later: Renovate or Dependabot opens version-bump PRs that the maintainer approves.

## 9. Versioning

- **Manifest schema**: `schemaVersion` is an integer. Clients check it and ignore or warn on versions they do not understand. Breaking changes bump it; additive changes do not.
- **Apps**: `version` tracks the packaged upstream version. If only the packaging changes, an optional `revision` field increments.
- **Index**: carries its own `schemaVersion` and `generatedAt`.

## 10. Security considerations

The repo is curated, so v1 relies on human review as the main control. Even so:

- Pin images by digest where practical so reviewed content stays what was reviewed.
- Flag risky settings in the manifest (privileged containers, host network, device passthrough). Consumers can surface these as warnings during install.
- Require SHA-256 for every QEMU image.
- Consumers should fetch over HTTPS and may pin a specific repo commit for reproducibility.
- Later options: signed commits or tags, signed `index.json`, and a `risk` field computed by CI.

## 11. Open questions

- Final category list.
- Whether to support multi-service dependencies (for example, an app that needs a shared database entry).
- Whether to allow app-level update hooks or migrations.
- Whether consumers need aarch64 variants per app, and how to express them.
- Hosting the index: raw GitHub URL versus GitHub Pages.
- Whether to add a lightweight static website generated from the index.

## 12. Roadmap

1. **v0.1**: JSON Schema, three or four seed apps (two Compose, one QEMU), index builder, CI validation.
2. **v0.2**: Contribution docs and review checklist, pinning and lint checks, icons.
3. **v0.3**: Reference consumer (small CLI) that installs a Compose app from the index.
4. **v0.4**: QEMU translation reference, update detection, Renovate integration.
5. **v1.0**: Frozen `schemaVersion: 1`, documentation site, public announcement.
