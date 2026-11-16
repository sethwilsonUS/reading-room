# ADR 0009: Bounded derived storage without changing research quality

Status: accepted

## Context

Semantic retention already keeps current and previous generations. Interrupted
builds and interrupted deletions can nevertheless leave unrecognizable files,
which conservative cleanup must preserve. Structure-analysis results also
accumulate across analyzer revisions and source/profile changes. Each semantic
generation has historically duplicated a retained immutable snapshot database.

These derived copies are distinct from durable curation, source identities,
authorizations, spending records, saved selections, historical citation extents,
and paid embedding caches. Reducing vector precision, changing the model, or
discarding paid work is outside a quality-preserving storage change.

## Decision

- Store structure patches and reports as atomic gzip JSON, preserving the
  complete data. Continue reading legacy JSON. Convert recognized legacy files
  during maintenance; do not remove the old file until the replacement is safe.
- Determine structure-cache retention from the exact generations actually
  retained on disk. Missing or inconsistent references defer reclamation.
- Let new semantic generations reference immutable snapshot databases directly.
  Continue reading the old generation-local mapping layout. Retention protects
  every snapshot referenced by a retained generation.
- Record application ownership before creating temporary semantic files and
  before deletion. Keep the ownership record until completion, so cleanup can
  resume after a crash without inferring ownership from an unfamiliar filename.
- Keep semantic-writer, library-writer, and semantic-reader lock ordering.
  Successful publication remains successful when optional maintenance defers.
- Provide a read-only `storage` report and explicit `storage compact` command.
  Neither command indexes source text or invokes providers. Report skipped and
  deferred work instead of silently claiming all space has been reclaimed.

## Consequences

Storage shrinks through lossless representation and removal of recognized
obsolete derivatives. Current and rollback search data, original books, and
durable history remain available. Legacy map duplication disappears as those
generations retire; it is not rewritten during a read. Unknown files remain an
explicit maintenance concern. Compression adds local CPU work, and rollback to
an older application may require regenerating disposable structure caches.

CI still tests the oldest and newest supported Python versions. Frontend,
browser, packaging, and full source-install checks run once per workflow rather
than once per interpreter; interpreter-specific Python contracts run on both.
