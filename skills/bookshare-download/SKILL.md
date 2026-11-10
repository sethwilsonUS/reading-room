---
name: bookshare-download
description: "Download user-authorized Bookshare books, choose useful text formats, verify editions and contents, name files meaningfully, and intake them into a configured Reading Room library. Use after a selection or direct download request."
---

# Bookshare Download

Acquire the authorized books and leave a verified, clearly named, searchable
local collection. Discover the configured library root through the sibling
[library-search launcher](../library-search/scripts/library) and `doctor --json`;
`~/Documents/Reading Room` is only the fallback. Honor a different requested destination or
format. Every `library` example means that absolute skill-relative launcher.
This skill does not download books merely
because it was invoked without an identifiable title or selection.

## Resolve the authorized set

Use the current request and preceding shortlist to resolve exact titles,
Bookshare IDs, editions, and volumes. “Download all recommended books” includes
the recommendation set, not every mentioned alternative or search result.
A direct request to download a title or a defined recommended set is sufficient
authorization; do not ask for redundant confirmation. If the intended set or
edition is materially ambiguous, resolve that before acquiring it. Use
[bookshare-find](../bookshare-find/SKILL.md) for discovery when needed.

Use [library-search](../library-search/SKILL.md) to check current index freshness
and local holdings before downloading; synchronize when needed. Check
title/author, edition, volume/coverage, Bookshare IDs, and acquisition records,
then inspect ambiguous source matches. Skip a usable identical local edition
and provide its link. Resume incomplete extraction or indexing of an existing
download rather than downloading it again. Do not silently replace an older
edition or existing file; keep distinct editions clearly labeled when requested.

## Select and obtain the format

Read [format and acquisition guidance](references/formats.md). Prefer a usable
text format that preserves useful structure, notes, and images. EPUB is the
default when EPUB and Word appear comparable; Word is equally valid when it
better preserves the needed content or is the available usable option. Avoid
conversion and duplicate-format downloads without a concrete benefit.

Prefer a suitable available Bookshare connector; otherwise use supported
browser tools and the user's signed-in session, honoring the chosen browser.
Inspect current controls and available formats. Do not change account-wide
preferences to acquire one book, invent download URLs, extract authentication
cookies, or bypass account/region restrictions. Ask for browser sign-in only
when needed; keep working on independent checks meanwhile.

Start the selected downloads through the normal interface. Track each title,
format, and status in the download history; preparation is not a saved file.
Follow the completed download action when ready, then locate and verify the
actual local file. Reuse a pending or completed request on retry instead of
creating duplicate download jobs. If preparation or access remains unavailable,
finish the other authorized books and report the precise outstanding items.

Keep original downloads in a stable source location, normally under
`~/Downloads/Bookshare/`, and do inspection/staging outside the configured library.
Use observed downloaded paths and collision-safe names; do not assume a browser
saved to a guessed filename. Retain the original package for verification.

## Verify and rename from the actual book

Use the read-only [inspection helper](scripts/inspect_book.py), a suitable
format reader, or both to inspect package integrity and relevant text. Run the
helper's `--help` for bounded inspection and literal-query options. Read the
publisher title/copyright pages and contents, not just the Bookshare wrapper or
file properties. Verify actual author/editor, title, edition, volume, and
coverage. Record any catalog mismatch separately. If the downloaded content
is a different unapproved work or volume, keep it out of the completed shelf,
report the mismatch, and look for the requested one.

**Rename every acquired book meaningfully.** Use this general shape, adapting
to the actual content and existing shelf conventions:

```text
Author or Editor - Short Title - Edition or Volume/
  Short Title - Complete Book.epub
```

For split Word files, preserve reading order and use descriptive boundaries:

```text
Short Title - 001 - Front Matter and Introduction.docx
Short Title - 002 - Genesis 1–11.docx
Short Title - 003 - Genesis 12–25.docx
```

Use verified chapter/section ranges or concise topics for the real contents.
If a boundary cannot be established, use a truthful “Part 003” label rather
than inventing one. Include edition/volume in filenames when needed to keep
files identifiable outside their folder. Clean path separators and illegal
filename characters; keep names manageable. Preserve every source part and
its original-member-to-final-name mapping. Do not silently merge, reorder,
omit apparently blank parts, or rewrite book text just to improve filenames.

Inspect representative body text, notes, tables, and relevant captions or images.
An opening sample is not a completeness check. Check start/end boundaries and
expected parts; a successful ZIP check alone does not establish readable text,
correct identity, or intact illustrations. Preserve originals byte for byte
when renaming/copying and verify hashes after placement.

## Integrate for future research

Read [shared-library storage and catalog rules](references/library.md) before
placing books in the configured library or updating its catalog. Keep originals,
provenance, and semantic filename mappings; do not overwrite an existing book
or manifest. Shared intake validates this acquisition; historical catalog
compatibility is documented separately.

Complete shared-library intake as part of each authorized acquisition. Follow
[source-neutral intake](../library-search/references/intake.md), which owns the
current context format, commands, automatic-review policy, and resume workflow.

1. Preserve verified placement, file hashes, reading order, retained source
   packages, and member-to-final-name mappings. Supply `provider: "bookshare"`,
   the actual record ID, observed catalog metadata, and bounded inspection
   evidence in the shared intake context. Keep catalog claims and actual-copy
   observations separate. Do not relabel a print ISBN as an export identifier.
2. Run `library intake --path /absolute/book/folder --context /absolute/context.json --json`.
   This registers durable intake using the library's already-approved settings.
   Automatic metadata review follows the saved future-intake default; explicitly
   use `--review-metadata`, `--no-review-metadata`, or `--local-only` when the user
   requests an override. New libraries have paid features off. A download request
   does not authorize enabling paid features or changing spending limits. It returns a
   stable intake ID and progress URL. Ordinary `sync` alone does not authorize
   this review. Do not bypass intake with direct `annotate` enrichment.
3. Use `library intake status INTAKE_ID --json` and the returned progress page
   to check extraction, exact search, semantic indexing, and review separately.
   The local service reviews book details first and tracks every indexed
   heading. The book can be searchable while section review is still running.
   Do not require the foreground download conversation to remain open.
4. When exact search is ready, verify a distinctive passage with exact search,
   reading, and `cite`; report honest extraction, contributor, and locator gaps.
   Add newly discovered evidence to a scoped review rather than overwriting
   user edits. Intake preserves uncertainty and handles source-supported
   corrections through its stricter automatic eligibility policy.
5. Report saved file links, acquisition identity, text/semantic readiness,
   automatic changes and remaining questions, and the intake progress link.
   Say “review still running” when appropriate; do not claim that every section
   was completely inspected merely because it received an outcome.

When authorized, review uses the existing OpenAI key loader, `gpt-6-luna` with low
reasoning, and the user's accepted monthly allowance shared with indexing and research. Budget
or provider pauses retain progress and require explicit Resume; a new month
alone does not restart paid work. Never create ad hoc API calls or another
download to repair intake. Supported automatic changes remain assistant-origin
and provisional, with evidence and Undo; personal verification is separate.

Failures are resumable by stage. Preserve verified files and acquisition
records, resume only the unfinished intake stages, and do not start
another download to repair an index. The shared state belongs in
the library root's `.library`, outside individual book folders; original files stay
unchanged. Use bounded reads with source/member/section locators. Cite the actual
contributor, edition, and chapter/section or verified printed page; file sequence
numbers and reflowed pages are not printed-page citations.

Finish with a concise list or count of saved books, descriptive folder/file
links, formats, meaningful edition corrections, already-present items skipped,
and any incomplete acquisition, indexing, or review stage.
Claim success only for files
that exist and were checked. Download permission is not permission to publish,
upload, or distribute the source books.
