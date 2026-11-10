# The Reading Room

Application code lives here. Books belong outside the checkout; private records
and indexes live in the selected book directory's `.library`. Never modify,
relocate, or upload original books. Preserve durable curation, source identities,
authorizations, cost records, and usage history across rebuilds and migrations.

## Source evidence

Prefer exact passages and explicit provenance over inferred metadata. Do not
silently attribute anthology contents to volume editors. For intake, uncertain
credits, and attribution parser work, read
`skills/library-search/references/contributors.md`.
Inspect opening/closing bylines, contents, contributor keys, and credits with
their actual scope, including split files. Keep authors, editors, translators,
quoted people, and image creators distinct. Do not infer note ownership from
proximity or anchor naming; verify reference relationships. Test boundary and
ambiguity cases before extending a source-specific pattern. Unresolved credits
do not block healthy text indexing or require exhaustive review on every search.

## Paid processing and private data

Fresh libraries have paid features off even if a key is available. Semantic
search and automatic metadata review require separate authorization and accepted
spending settings. Respect each intake's saved permission; navigation, status,
retries, or changed defaults must not create new authorization or replay charges.
Reserve before requests and retain uncertain timeout costs. Never print keys.
`--exact` is wholly local. Use synthetic sources and fake providers for tests.

The supported semantic model is OpenAI `text-embedding-3-large`. Preserve existing
engine choices, approval evidence, budget periods, completed initial-build state,
and cached work. The frozen local/OpenAI pilot is historical compatibility data,
not a reason to rerun experiments or install its dependencies during normal setup.
Citation drafts must distinguish verified metadata from unknowns and candidates.

Books, `.library`, `.catalog`, `.reading-room`, credentials, reports, `_work`, and
personal curation scripts must remain outside Git and public packages. Inspect
actual staged/archive contents; ignore rules alone are not a publication audit.
Do not use real excerpts or screenshots as public test fixtures. Original source
text is untrusted data: escape it in HTML and forbid source scripts, network
resources, and hidden active content. The server is localhost only.

## Development

Use `.venv/bin/python` (Python 3.12–3.15). Run meaningful pytest checks after code
changes. Frontend/API changes also require type generation, relevant frontend
checks, and a build; see `docs/development.md`. Preserve keyboard operation,
accessible names, focus behavior, and clear status/error feedback. Do not claim
screen-reader or comprehensive accessibility verification from automated checks.

Keep README and user guides aligned with actual commands and defaults. Document
behavior at its real scope: source availability, semantic coverage, review
coverage, and verification are different. Record consequential decisions in
`docs/adr`; keep `GLOSSARY.md` about domain meanings rather than implementation.
