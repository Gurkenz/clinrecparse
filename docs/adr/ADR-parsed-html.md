# ADR: parsed HTML

Decision: parsed HTML is preserved as sanitized HTML, not converted to Markdown
as a canonical source.

Context:

- Clinical recommendations contain tables, images, lists, and inline formatting.
- Markdown loses structure and is not safe as a canonical round-trip format.

Consequences:

- Frontend uses normalized HTML.
- Search uses plain text extracted from normalized HTML.
- Validation compares visible text from raw HTML and normalized HTML.
- Unsafe constructs are removed: scripts, event handlers, and `javascript:`
  URLs.

