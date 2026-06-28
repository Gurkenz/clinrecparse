# Frontend export

`clinrec parsed-export` writes frontend payloads to `frontend/`:

- `documents/{CodeVersion}.json`
- `assets/by-sha256/*`
- `manifest.json`

Document payloads contain metadata, table of contents, ordered sections with
safe normalized HTML, table metadata, image metadata, and warnings.

Frontend HTML must not contain base64 images, script tags, event handlers, or
`javascript:` URLs. Local decoded assets are content-addressed by SHA-256.

