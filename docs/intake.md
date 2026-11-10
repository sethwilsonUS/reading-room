# Acquisition and intake

**Acquisition** is obtaining a particular copy and recording where it came from. **Intake** prepares that copy for reading, search, and any authorized review. Downloaded, readable, semantically indexed, and metadata-verified are separate states.

The supported source formats are **EPUB** and **DOCX**. Keep books outside the application checkout, inside your configured library root. Original package bytes are not changed by intake, indexing, or correction.

## Manual intake

Keep the original download and place a byte-identical copy in a separate folder for that book. For example:

```text
ReadingRoom/
  Example Book/
    Example Book.epub
  .library/              private application state
```

With the Reading Room open (Reading Room.app, `./bin/library app`, or `./bin/library serve`), request intake from a terminal:

```sh
./bin/library intake --path "$HOME/Documents/Reading Room/Example Book" --local-only --json
```

Replace this path with the book folder under the root you selected during setup. `--path` selects one book file or folder, not the entire library. The result includes the intake ID and status URL.

The worker prepares text first, then runs any authorized paid stages. A stopped server leaves work queued; closing the Reading Room window stops the server it started. Open **Intake** in the window or browser, or use:

```sh
./bin/library intake status --json
./bin/library intake status INTAKE_ID --json
```

Replace `INTAKE_ID` with the actual returned identifier. Refresh explicitly for updated progress; this preview does not poll automatically.

## Choose processing for an import

| Command option | Effect on this intake |
| --- | --- |
| No processing override | Use the semantic authorization and automatic-review default recorded for the library when the job is created. |
| `--review-metadata` | Request automatic metadata review for this intake, even if the future-import default is off. Accepted spending limits are required. |
| `--no-review-metadata` | Skip automatic metadata review; already-enabled semantic indexing may still run. |
| `--local-only` | Skip both paid stages for this intake. Do not combine it with review overrides. |

`--local-only` is an operation choice, not a permanent prohibition on indexing that book. A later explicitly requested full-library semantic sync can include it. Another book's intake does not silently pull an earlier local-only import into paid processing: its semantic stage covers the selected book plus unchanged files already represented in the published authorized index. See [AI setup](ai-and-privacy.md) before opting in.

Each job records its resolved permissions. Changing a library default does not silently upgrade earlier imports. Request metadata review later for an existing intake with:

```sh
./bin/library intake review INTAKE_ID --json
```

This reuses the intake identity and completed results. It is not a reason to download the book again.

## Supply context when it helps

An optional JSON context file can carry provider claims, ordered files, retained-source provenance, and inspection evidence. It is private acquisition data; do not commit it to this repository.

A small local-copy example is:

```json
{
  "provider": "local",
  "source_label": "My local copy",
  "metadata": {
    "title": "Example Book",
    "authors": ["Example Author"]
  }
}
```

```sh
./bin/library intake --path "/absolute/library/Example Book" --context "/absolute/private/context.json" --local-only --json
```

Supplied metadata is a claim, not automatic verification. The [intake reference](../skills/library-search/references/intake.md) documents richer context, including file mappings and evidence.

For a split DOCX export, keep all parts of the selected copy together and supply their exact order when names are insufficient. Do not mix an EPUB and DOCX alternative or multiple editions in one intake. Ambiguous copies and unexpected adjacent files need an explicit selection; the library does not guess which to merge.

The [inspection helper](../skills/bookshare-download/scripts/inspect_book.py) can examine DOCX, EPUB, and download ZIP packages without modifying them. A ZIP download is a container: place its supported book members before intake, preserving their relationship to the retained archive. The helper alone does not place files or establish that every page is complete.

## Bookshare-assisted intake

Use the [Bookshare skills](codex.md#bookshare-workflow) to find a copy, authorize a download, inspect the actual export, and prepare intake context. A catalog title or ISBN may describe a related print format rather than the accessible copy; preserve discrepancies instead of silently replacing the book's own evidence.

Contributor checking has its own [guide](../skills/library-search/references/contributors.md). Look at bylines, contents, contributor keys, and scoped credits. A volume editor is not automatically the author of a chapter, poem, or commentary. Missing credits do not block healthy text indexing.

## Pause, recover, and undo

```sh
./bin/library intake pause INTAKE_ID --json
./bin/library intake resume INTAKE_ID --json
./bin/library intake undo INTAKE_ID --json
```

Pause takes effect between bounded stages; an already-sent request cannot be recalled. Resume does not grant new permissions. Budget and provider pauses require explicit resume, including after the calendar month changes. Uncertain interrupted requests retain their cost reservations rather than silently replaying.

Undo reverses eligible automatic corrections while preserving later user edits. It does not delete the book, undo charges, or erase research history. Some findings need evidence and remain in the guided review rather than being applied automatically.

Changed source files require review of the job's scope. Preserve originals and investigate the change; do not repair an indexing problem by redownloading or directly annotating around the intake workflow.

## Intake versus sync

`sync --exact` discovers and indexes local text without paid work. Ordinary `sync` also updates the configured semantic index when authorized. Neither command creates permission for automatic metadata review. Intake adds acquisition context, a scoped job, and an explicit account of processing progress.

Text readiness does not imply complete semantic coverage or a verified citation. Check status and read the source before using a result.
