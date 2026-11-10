# Everyday use

The Reading Room separates finding a passage from verifying what it supports. Search is a starting point: read the surrounding argument, check the actual contributor and edition, and resolve citation warnings before quoting.

## Search and browse

Open the shelves to browse books by title or recorded creator. Creator search includes book-level authors, editors, and translators; it does not make every chapter contributor a book author.

Use a specific phrase for a direct wording search. With semantic search enabled, ordinary search also finds related ideas and reports its actual coverage. Results distinguish direct matches from possible connections. A plausible connection is not evidence that a source makes your exact argument.

```sh
./bin/library status --json
./bin/library search 'memory and place' --exact --json
./bin/library books 'poetry' --json
./bin/library browse --classification themes=water --json
```

For agents and scripts, `--with-context` returns the same results as the interface's search, each with its surrounding source text and a citation draft, in one bounded JSON response:

```sh
./bin/library search 'memory and place' --with-context --json
```

Each hit's `context` separates the matched `passage` from the text `before` and `after` it. Its `citation` carries the notes, bibliography entry, and the warnings and missing fields to resolve before submission. See the [command reference](../skills/library-search/references/commands.md#search-with-context-in-one-call) for the size limits.

Status checks file size and modification time without rereading every original. Synchronize after adding, changing, or removing files. Stale candidate passages are hidden rather than presented as current evidence.

Search submission is explicit. Returning to a prior search restores available results without repeating a paid request. If the browser cannot retain that state, submit the search again intentionally.

## Read in context

Open a result to read its surrounding section. Supported books provide expandable contents and complete reading units with internal headings and verified note links. When boundaries are uncertain, the app preserves a labeled excerpt view rather than pretending it found a complete chapter.

The CLI exposes the same source context. Replace the uppercase identifiers with IDs from the previous result:

```sh
./bin/library read PASSAGE_ID --context 3 --json
./bin/library book WORK_ID --json
./bin/library contents WORK_ID --json
./bin/library read-unit UNIT_ID --json
```

For long units, follow the returned continuation information. Source structure, text availability, semantic coverage, and verified bibliography are separate measurements. An exported heading does not prove authorship, and a source mention does not necessarily mean sustained coverage of a subject.

## Create citation drafts

Choose **Cite this passage** or a reading unit's citation action for Chicago notes-and-bibliography drafts. The app provides a full note, shortened note, and bibliography entry. Copy or export plain text, Markdown, or JSON.

```sh
./bin/library cite PASSAGE_ID --json
./bin/library cite PASSAGE_ID --format note
./bin/library cite-unit UNIT_ID --json
```

Read the warnings. Unknown contributors, provisional publication facts, and missing page locators remain visible. An editor is not automatically the author of an essay. Extraction block numbers and reflowed screen positions are navigation aids, not printed page numbers.

Verify the edition you used. Where an edition has no printed page markers, use a supported chapter or section locator rather than inventing a page. The [contributor guide](../skills/library-search/references/contributors.md) explains scoped credits; the [glossary](../GLOSSARY.md) distinguishes a reading unit, indexed passage, and contribution.

Exact quotation selection currently uses mouse highlighting. Select contiguous source text, choose **Cite selected text**, and confirm the reconstructed quotation. Saved selections use source coordinates and fingerprints; a changed source makes the old selection unavailable for review instead of moving it to similar wording. A dedicated keyboard/screen-reader range-selection workflow is not part of this preview. Passage and whole-unit citations remain separate routes.

## Correct information and verify evidence

Use **Review and edit information** from a book, section, or citation. The review explains what a change affects and shows its evidence. Book publication details belong to the book review; a section's contributor belongs to that section's scope.

The guided view presents recommendations one at a time. Navigation does not accept a suggestion. Add chosen changes to the draft, then use **Save changes** to apply the selected batch. The full editor is available under **View and edit all information**.

Keep these distinctions:

- A proposed correction is unsaved.
- A saved correction can still be provisional.
- Verification records that you checked evidence for a particular value and scope.
- An automatic intake correction is assistant work, not your personal verification.

The same review is available in the terminal as `library correction` (`show`, `set`, `decide`, `apply`, and others; see the [command reference](../skills/library-search/references/commands.md#review-and-correct-metadata)). It shares the draft with the interface, so you can, for example, let an agent prepare changes and then review and save them in the window. Requesting paid AI suggestions is interface-only.

Manual drafts survive navigation separately from effective library metadata. Changed source material or intervening edits require rechecking them. Undo preserves later user work. AI suggestions are optional; manual editing remains available without paid features.

## Focus a research session

A research focus boosts preferred subjects or forms. It does not exclude other books; use explicit filters when you want a restriction.

```sh
./bin/library focus list --json
./bin/library search 'memory and place' --focus general-research --exact --json
./bin/library search 'memory' --filter form=poem --exact --json
```

The initial focuses are general-purpose. Optional theology/teaching examples live in [examples/focuses](../examples/focuses):

```sh
./bin/library focus set bible-study --file examples/focuses/bible-study.json --json
```

You can edit focuses and classifications in the interface. Existing user edits are retained; missing deleted focuses are not repeatedly reinstated as defaults. The **Subjects & connections** view also browses classified books and sections. A classification is a discovery aid, not a new factual claim about a source.

## Record research use deliberately

Reading or highlighting a passage does not record that you taught, quoted, or published it. Use the app's record-use action when meaningful use occurs. The states `inspected`, `offered`, `recommended`, `selected`, and `used` have different meanings.

A CLI example for an inspected work:

```sh
./bin/library usage record --event '{"kind":"inspected","work_id":"WORK_ID","project":"my-research","evidence":"Checked chapter 2 for the workshop"}' --json
```

Keep this history separate from book content and metadata. It is private research information, included in your library backup and excluded from public bug reports.
