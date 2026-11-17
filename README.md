# openappstore

A curated catalog of self-hosted app definitions. See [design.md](design.md).

- Browse `index.json` for the catalog.
- Each app lives in `apps/<id>/` with an `app.yaml` manifest.
- Validate and rebuild the index with `python scripts/build-index.py`.