#!/usr/bin/env python3
"""Validate app manifests and build index.json for openappstore."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

import yaml
import jsonschema

ROOT_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join("schema", "app.schema.json")
INDEX_SCHEMA_PATH = os.path.join("schema", "index.schema.json")
APPS_DIR = "apps"
MAX_ICON_BYTES = 256 * 1024
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


class Reporter:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, app, msg):
        self.errors.append(f"[{app}] {msg}")

    def warn(self, app, msg):
        self.warnings.append(f"[{app}] {msg}")

    def ok(self):
        return not self.errors


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(base, rel):
    return os.path.normpath(os.path.join(base, rel))


def sha256_folder(folder):
    h = hashlib.sha256()
    files = []
    for root, _, names in os.walk(folder):
        for n in names:
            full = os.path.join(root, n)
            rel = os.path.relpath(full, folder)
            files.append((rel, full))
    files.sort(key=lambda x: x[0])
    for rel, full in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        h.update(b"\x00")
    return h.hexdigest()


def resolve_binds_path(obj, path):
    """Resolve a dotted/bracketed path like qemu.network.forward[0].host."""
    cur = obj
    for part in re.findall(r"[^\.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
    return cur


def check_compose_vars(app, content, inputs, reporter):
    keys = {i.get("key") for i in (inputs or [])}
    found = set(VAR_RE.findall(content))
    for var in found:
        if var not in keys:
            reporter.error(app, f"compose references ${{{var}}} but no input with that key exists")


def check_pinning(app, compose_obj, reporter):
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "image" and isinstance(v, str):
                    img = v.strip()
                    if "@" in img:
                        return
                    if ":" not in img:
                        reporter.error(app, f"image '{img}' has no tag or digest (defaults to latest)")
                    else:
                        tag = img.rsplit(":", 1)[-1]
                        if tag == "latest" or tag == "":
                            reporter.error(app, f"image '{img}' uses 'latest' or empty tag")
                walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)
    walk(compose_obj)


def compose_lint(app, compose_path, inputs, reporter):
    if not shutil.which("docker"):
        reporter.warn(app, "docker not available; skipping compose lint")
        return
    env = dict(os.environ)
    for inp in (inputs or []):
        key = inp.get("key")
        if key:
            default = inp.get("default", "")
            env[key] = "" if default is None else str(default)
    # ensure placeholders
    for k in ("PORT",):
        env.setdefault(k, "0")
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_path, "config"],
            cwd=os.path.dirname(compose_path),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as e:
        reporter.error(app, f"docker compose config failed: {e.stderr.decode(errors='replace').strip()}")
    except Exception as e:
        reporter.warn(app, f"compose lint error: {e}")


def reachability(url, reporter_app, reporter):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                reporter.error(reporter_app, f"image URL not reachable: HTTP {resp.status}")
    except Exception as e:
        # try GET range
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status >= 400:
                    reporter.error(reporter_app, f"image URL not reachable: HTTP {resp.status}")
        except Exception as e2:
            reporter.error(reporter_app, f"image URL not reachable: {e2}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=ROOT_DEFAULT, help="repository root")
    parser.add_argument("--out", default=None, help="output index path (default: <root>/index.json)")
    parser.add_argument("--check", action="store_true", help="validate only, do not write index")
    parser.add_argument("--check-reachability", action="store_true", help="check QEMU image URLs")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    out = args.out or os.path.join(root, "index.json")
    apps_dir = os.path.join(root, APPS_DIR)
    schema = json.load(open(os.path.join(root, SCHEMA_PATH), encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    reporter = Reporter()
    entries = []

    if not os.path.isdir(apps_dir):
        print("no apps directory", file=sys.stderr)
        sys.exit(1)

    app_ids = sorted(d for d in os.listdir(apps_dir) if os.path.isdir(os.path.join(apps_dir, d)))

    for app_id in app_ids:
        app_folder = os.path.join(apps_dir, app_id)
        manifest_path = os.path.join(app_folder, "app.yaml")
        if not os.path.isfile(manifest_path):
            reporter.error(app_id, "missing app.yaml")
            continue

        # load manifest
        try:
            manifest = load_yaml(manifest_path)
        except Exception as e:
            reporter.error(app_id, f"invalid YAML: {e}")
            continue

        # schema validation
        errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
        for e in errors:
            loc = "/".join(str(p) for p in e.path) or "<root>"
            reporter.error(app_id, f"schema: {loc}: {e.message}")

        # folder name == id
        mid = manifest.get("id") if isinstance(manifest, dict) else None
        if mid is not None and mid != app_id:
            reporter.error(app_id, f"folder name '{app_id}' != id '{mid}'")

        if not isinstance(manifest, dict):
            continue

        runtime = manifest.get("runtime")
        inputs = manifest.get("inputs") or []

        # icon checks
        icon_rel = manifest.get("icon")
        if icon_rel:
            icon_path = resolve_path(app_folder, icon_rel)
            if not os.path.isfile(icon_path):
                reporter.error(app_id, f"icon not found: {icon_rel}")
            elif os.path.getsize(icon_path) > MAX_ICON_BYTES:
                reporter.error(app_id, f"icon exceeds {MAX_ICON_BYTES} bytes")

        # compose handling
        compose_content = None
        compose_obj = None
        compose_file_rel = None
        compose_path = None
        if runtime == "compose":
            compose_spec = manifest.get("compose") or {}
            if "file" in compose_spec:
                compose_file_rel = compose_spec["file"]
                compose_path = resolve_path(app_folder, compose_file_rel)
                if not os.path.isfile(compose_path):
                    reporter.error(app_id, f"compose file not found: {compose_file_rel}")
                else:
                    with open(compose_path, encoding="utf-8") as f:
                        compose_content = f.read()
                    try:
                        compose_obj = yaml.safe_load(compose_content)
                    except Exception as e:
                        reporter.error(app_id, f"compose YAML parse error: {e}")
            elif "inline" in compose_spec:
                compose_content = compose_spec["inline"]
                try:
                    compose_obj = yaml.safe_load(compose_content)
                except Exception as e:
                    reporter.error(app_id, f"compose YAML parse error: {e}")
                # write inline to temp for lint
                tmpdir = tempfile.mkdtemp()
                compose_path = os.path.join(tmpdir, "compose.yaml")
                with open(compose_path, "w", encoding="utf-8") as f:
                    f.write(compose_content)

            if compose_content is not None:
                check_compose_vars(app_id, compose_content, inputs, reporter)
            if compose_obj is not None:
                check_pinning(app_id, compose_obj, reporter)
            # lint
            if compose_content is not None:
                if compose_path is None:
                    tmpdir = tempfile.mkdtemp()
                    compose_path = os.path.join(tmpdir, "compose.yaml")
                    with open(compose_path, "w", encoding="utf-8") as f:
                        f.write(compose_content)
                compose_lint(app_id, compose_path, inputs, reporter)

        # qemu handling
        if runtime == "qemu":
            qemu = manifest.get("qemu") or {}
            # binds validation
            for inp in inputs:
                binds = inp.get("binds")
                if binds:
                    if resolve_binds_path(manifest, binds) is None:
                        reporter.error(app_id, f"input '{inp.get('key')}' binds to unknown path: {binds}")
            # reachability
            img_url = (qemu.get("image") or {}).get("url")
            if args.check_reachability and img_url:
                reachability(img_url, app_id, reporter)

        # builds entry if valid so far? We'll still build; errors handled at end.
        # compute hash regardless
        h = sha256_folder(app_folder)

        # icon path for index: relative to root
        icon_index = None
        if icon_rel:
            icon_index = os.path.join(APPS_DIR, app_id, icon_rel).replace(os.sep, "/")
        manifest_index = os.path.join(APPS_DIR, app_id, "app.yaml").replace(os.sep, "/")

        entries.append({
            "id": manifest.get("id", app_id),
            "name": manifest.get("name"),
            "version": str(manifest.get("version")) if manifest.get("version") is not None else None,
            "category": manifest.get("category"),
            "description": manifest.get("description"),
            "runtime": runtime,
            "icon": icon_index,
            "manifest": manifest_index,
            "sha256": h,
        })

    # validate index against schema
    index = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "apps": entries,
    }

    # validate index schema
    try:
        idx_schema = json.load(open(os.path.join(root, INDEX_SCHEMA_PATH), encoding="utf-8"))
        jsonschema.Draft202012Validator(idx_schema).validate(index)
    except Exception as e:
        reporter.error("<index>", f"index schema validation failed: {e}")

    # sort entries by id
    index["apps"].sort(key=lambda a: a["id"])

    # warnings as errors if strict
    if args.strict and reporter.warnings:
        reporter.errors.extend(reporter.warnings)
        reporter.warnings = []

    # print report
    for w in reporter.warnings:
        print("WARN:", w)
    for e in reporter.errors:
        print("ERROR:", e, file=sys.stderr)

    if not reporter.ok():
        print(f"\nValidation failed: {len(reporter.errors)} error(s).", file=sys.stderr)
        sys.exit(1)

    if args.check:
        print(f"Validation OK: {len(entries)} app(s).")
        return

    # Preserve generatedAt when the catalog content is unchanged, so repeated
    # runs (and CI checks) stay deterministic.
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("apps") == index["apps"]:
                index["generatedAt"] = existing.get("generatedAt", index["generatedAt"])
        except Exception:
            pass

    with open(out, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {out} with {len(entries)} app(s).")


if __name__ == "__main__":
    main()
