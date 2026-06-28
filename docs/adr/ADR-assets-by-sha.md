# ADR: assets by SHA-256

Decision: decoded local assets are stored under `assets/by-sha256/{sha}.{ext}`.

Context:

- Raw documents may embed repeated base64 images.
- Frontend payloads must not carry base64 blobs.
- Asset identity must be deterministic and deduplicated.

Consequences:

- Identical images share the same asset path.
- Parsed image records point to `asset_sha256` and `asset_path`.
- Validation fails unresolved local image references.
- External images are inventoried but not downloaded by the parsed layer.

