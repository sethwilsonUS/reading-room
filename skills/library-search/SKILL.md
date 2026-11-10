---
name: library-search
description: "Search a local Reading Room book library for passages, context, and interdisciplinary connections; read and verify results, maintain research focuses and evidence-backed metadata, and record source use. Use for book-based research, teaching, and writing."
---

# Shared Library Search

Prefer usable local sources in the configured library. Resolve the absolute
path of [scripts/library](scripts/library) relative to this SKILL.md's location
provided by the agent host. Invoke that launcher; every `library` command below
is shorthand for its absolute path, not a command assumed to be on PATH. It
finds the checkout and virtual environment even through installed skill symlinks.
For the other two sibling skills, use `../library-search/scripts/library`.

Run `library doctor --json` to confirm the configured root and setup. Root
precedence is explicit `--root` before the command, `LIBRARY_ROOT`, the checkout's
saved setup binding, then `~/Documents/Reading Room`. Do not guess a personal path. The index
is shared across projects; a book's original purpose does not restrict later
use. Leave original books unchanged. Private records live in the root's `.library`.

The interface normally uses `http://127.0.0.1:18765`; consult diagnostics for a
custom address. Login startup is optional. Intake work needs a running service:
reuse an existing one, or start `library serve` (browser) or `library app`
(native window, which reuses a running server), avoiding duplicate servers.
Search, reading, citations, and corrections work without it. Neither skill
invocation nor an available key authorizes AI setup.

## Search a changing shelf

1. Run `library status --json` at the start of a research session. Review
   freshness, extraction failures, pending metadata, semantic coverage, and
   structural outcomes. Normal sync checks structure automatically;
   `needs_review` indicates uncertain boundaries, not necessarily missing text.
   Run `library sync --json` when freshness indicates additions, changes,
   removals, pending curation/index changes, or an absent index. Do not reopen
   every original or read the entire catalog on every query. Synchronization does not require the acquisition
   audit or retained download archives.
2. Search the actual question using a suitable focus and mode. Begin with
   direct concepts or a precise phrase, then try meaningful synonyms or an
   exploratory question when useful. Use explicit filters only when the user
   or question requires exclusions; focus preferences rank rather than exclude.
   When explicitly enabled, ordinary search combines exact text with the
   configured OpenAI large semantic index. Otherwise it uses local exact search.
   Use `search ... --exact --json` for wholly local retrieval, or when
   semantic service is unavailable. Check actual indexed coverage; a selected
   engine does not mean every new or changed book is already embedded.
3. Prefer `search ... --with-context --json` to get each hit's surrounding
   source text and citation draft in one call (see
   [commands](references/commands.md#search-with-context-in-one-call)).
   Follow a search result’s `read_url` or use `library read-unit UNIT_ID --json`
   for its complete containing unit. Use `library contents WORK_ID --json`
   for the hierarchy. Follow explicit continuation cursors; one returned page
   may not contain the whole unit. For unresolved boundaries, use the labeled
   excerpt through `library read ID --context 3 --json`. Inspect the
   original when extraction, attribution, pagination, notes, or images remain
   uncertain. A snippet, generated profile, or similarity score is not evidence.

For newly acquired or explicitly selected existing EPUB/Word books, follow
[source-neutral intake](references/intake.md). It owns registration, automatic
provisional enrichment, progress, and resumption. Ordinary research and sync do
not authorize a new intake or an existing-library backfill.

At new-book intake, after structural extraction changes, or when a book's
contents look implausible, use the bounded [structure check](references/structure.md).
A substantial book indexed entirely as “Front matter,” or with far fewer
sections than its printed contents, needs inspection even when freshness,
text extraction, and semantic coverage are healthy. Do not repeat this audit
on every ordinary search.

Use the absolute skill-relative launcher described above. Read
[commands and curation](references/commands.md) for invocation examples,
research-focus editing, classifications, and usage recording.

Use `library focus list --json` to discover the user's actual research focuses.
Do not assume optional example focuses have been installed. Inspect each saved
focus rather than assuming its preferences never change.
Use `focused`, `balanced`, or `exploratory` to suit the question. The default
result budget is up to twelve direct hits and five possible connections;
request further bounded searches when they materially help.

## Verify meaning and attribution

Keep direct results and **possible connections** distinct. Explain a proposed
connection in terms of the actual passages and its limits. Semantic proximity
does not establish historical influence, authorial intention, shared doctrine,
or primary commentary coverage. Incidental verse mentions are not commentary
on that passage. Unknown metadata should prompt inspection, not exclusion.

Verify author or contributor, title, edition or volume, translation, and source
location. Anthology editors are not automatically the authors of poems or
essays; commentary contributors may differ by section. Read surrounding
argument and consequential notes, including across split Word files. Preserve
poetry lineation when checking wording. Do not turn extraction ordinals or
reflowed pages into printed-page citations.

For edited collections, uncertain credits, or new-book intake, follow
[finding and preserving contributors](references/contributors.md): check
opening and closing bylines, contents, rosters, and credits; establish the
contribution's boundaries and record evidence-backed attribution. Use bounded
checks of the material at hand. Unknown contributors remain visible as gaps,
without preventing text research or requiring a whole-book audit per query.

Use [text types and mixed passages](references/text-types.md) when Scripture,
commentary, poems, captions, or editorial material need distinct labels or
credits within one result. Classification is partial: unknown material stays
searchable, and a text-type filter can exclude unclassified relevant material.

Use `library cite-unit UNIT_ID --json` for a whole reading unit. For exact
wording, create a selection using source block IDs and Unicode code-point ranges,
confirm its reconstructed quotation, and use `library cite-selection SELECTION_ID
--json`. Browser highlighting provides explicit citation and recording actions.
Never locate a saved quotation by matching text or reuse the first retrieval
chunk’s credit as the whole chapter’s credit. See [commands and curation](references/commands.md)
for selection examples. Existing passage references remain supported through
`library cite PASSAGE_ID --json`. Each citation operation returns a
Chicago notes/bibliography full note, shortened note, and bibliography entry,
with missing-field and locator warnings. Resolve those warnings from the actual
edition before treating a draft citation as ready. Bibliographic completeness
is separate from subject-profile completeness. Never replace a missing
contributor, publisher, edition year, or printed page with a plausible guess.
Keep chapter/poem authors, volume editors, and translators distinct; an
accessible export's download date or Bookshare notice is not its edition year.

Use the output in the task's requested citation format, retaining descriptive
absolute local-file links labeled as the user's library. Chicago is the library's
academic default, not a requirement to turn every lesson or Substack brief into
a footnoted paper. Honor an instructor's specified Chicago/Turabian edition,
SBL, or other style. Index IDs and local reader links help resume research;
they do not replace scholarly attribution or become public source URLs.
Retain stronger canonical-source and rights requirements. Keep copyrighted
excerpts brief, and do not distribute source books.

Use https://app.logos.com for missing resources, additional perspectives,
original-language tools, or unresolved questions. Prefer its web interface with
Limited View enabled; use the desktop app for a specific web gap. A separate
Logos pass or duplicate lookup is unnecessary when local sources meet the need.
The local shelf need not be exhausted before pursuing a useful external lead.
Use authoritative current, institutional, and primary sources where appropriate.

## Coverage, curation, and research memory

An empty result can mean a holdings gap, unsupported extraction, stale data, or
an unsuccessful query. Check status and sensible synonyms before reporting the
distinction. Never manufacture direct support from lateral results. A topic
such as Enneagram may honestly have no dedicated local books yet.

The supported optional semantic model is OpenAI `text-embedding-3-large`.
Ordinary `sync` updates exact search and an explicitly configured semantic index;
use its stage/coverage report to distinguish text-ready material from semantic
work still pending. Resume failed indexing without reacquiring the book.
Requests remain subject to the user's accepted application spending limits.
New setup offers $10 initial-build and $10 monthly limits for explicit acceptance;
existing libraries may have different limits. Do not bypass those limits with
ad hoc embedding calls. API failure or a ceiling must leave exact search,
reading, and citations available. The frozen pilot remains a separate comparison
record; report live full-library coverage rather than assuming pilot coverage.

Classifications generated by Codex are provisional, supported by inspected
front matter, contents, and selected passages. For intake, use the shared intake pipeline rather than direct annotations.
Outside intake, use `annotate` for explicitly requested, evidence-backed
corrections or additional labels, preserving user corrections and uncertainty.
`library correction` runs the interface's review workflow; draft freely, but
`apply` only corrections the user asks for or approves, and never mark a value
verified on the user's behalf ([commands](references/commands.md#review-and-correct-metadata)).
Taxonomy terms can overlap; do not move books into subject folders. The browser
interface offers the same focuses and editing operations as the command.

Record meaningful research use separately from source content, with an explicit
`target_scope` of `unit`, `selection`, or legacy `passage`. Opening or highlighting
text alone does not record use. Use the events
`inspected`, `offered`, `recommended`, `selected`, and `used` accurately, with
project and evidence links. A suggestion is not a selection; a selected item
is not proof that it was taught or published. Recent use informs discovery
without banning worthwhile repetition. Preserve each project's own research
record and approval gates.

If the command or index is temporarily unavailable, report the material limit,
use bounded format-aware inspection of promising files, and continue with
other available sources. Do not claim that an unavailable index was searched.
