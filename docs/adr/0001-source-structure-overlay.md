# Normalize structure separately from extracted text

Raw EPUB and Word exports can flatten headings, restart levels at split-file
boundaries, and separate notes from the contributions they belong to. The
library therefore derives a source-evidenced outline and explicit reading-unit
membership before contributor analysis, while preserving extraction caches,
source text, original levels, and locators.

Outline nodes, reading units, and citation targets are different concepts.
Neither visual heading depth nor adjacency proves authorship. Explicit block
membership prevents a unit from swallowing intervening essays when its notes
occur later. Uncertain structure remains readable and is reported for review.
The same versioned analysis runs for existing books and future intake; no
separate one-off import path may silently bypass it.

Declared EPUB navigation may lead to a source index containing an opening title
and only linked labels. Such an index can supply additional section boundaries
when each label matches a unique anchored source heading within the index's
source interval. The index becomes an outline grouping so its linked sections
can have separate reading units. Conflicting parents, prose-bearing index
candidates, and numeric links into chapter text do not establish new headings.
These boundaries establish navigation, not note ownership or contributor credit.
