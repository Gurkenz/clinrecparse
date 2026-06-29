# Frontend export

`clinrec parsed-export` writes frontend payloads to `frontend/`:

- `documents/{CodeVersion}.json`
- `assets/by-sha256/*`
- `manifest.json`

Document payloads contain metadata, table of contents, ordered sections with
safe normalized HTML, table metadata, image metadata, and warnings.

Frontend HTML must not contain base64 images, script tags, event handlers, or
`javascript:` URLs. Local decoded assets are content-addressed by SHA-256.

`clinrec parsed-build-showcase` writes a showcase frontend package to
`data/showcase/{CodeVersion}/frontend/`:

- `document.json`
- `assets/by-sha256/*`
- `manifest.json`

Showcase `document.json` uses schema `0.2-pilot`. It contains a TOC, ordered
sections, normalized HTML, table IDs, image occurrence IDs, asset records, and
warnings. It is intended for local preview and integration review, not as a
production UI contract.

Frontend consumers must resolve local paths relative to the frontend package.
The normalized HTML must not contain base64 image data, unsafe tags, event
handlers, or `javascript:` URLs.
