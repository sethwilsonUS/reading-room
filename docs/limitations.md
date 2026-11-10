# Support and known limitations

This is a source-installed macOS technical preview, maintained as a personal project. The [release record](release.md) distinguishes automated results from manual verification. There is no response-time guarantee or claim that every source package renders perfectly.

## Supported scope

- Python 3.12–3.15 and Node.js 22.22.2+ within 22.x, 24.15.0+ within 24.x, or 26.0.0+ for source setup/builds; 24.x is recommended. Normal use runs the Python server.
- Local EPUB and DOCX sources, including supported split Word exports.
- Exact-text retrieval and optional OpenAI `text-embedding-3-large` semantic retrieval.
- Optional evidence-backed metadata review, with provisional corrections and manual verification.
- Native macOS window (`library app` / Reading Room.app), browser UI, and CLI, plus a tested Codex-oriented skill workflow.
- The window runs under the Python runtime, so its Dock icon and menu-bar name are Python's rather than the Reading Room's. A packaged, signed application is not part of this preview.

Other operating systems are experimental. Native Windows is not supported: the current locking and launchers use Unix facilities. The older local embedding/pilot paths remain internal compatibility functionality, not a second supported setup route.

## Accessibility

The UI uses semantic landmarks, labeled controls, keyboard focus states, and announced status feedback. Automated checks cover representative navigation, focus, reflow, enlarged text, and accessible-name behavior. These checks do not establish comprehensive WCAG conformance or replace screen-reader listening.

**Exact quotation selection currently requires mouse highlighting.** A dedicated keyboard/screen-reader range-selection interface is deferred. Use passage or whole-reading-unit citation actions instead when appropriate; these identify different scopes and are not a substitute for verifying the wording you quote.

The targeted Safari keyboard pass covered search, contextual reading, whole-unit citation, and intake navigation. **VoiceOver speech/listening has not been verified and remains a follow-up for this technical preview.** No screen-reader or comprehensive accessibility conformance is claimed. See the [release verification record](release.md#verification-record) for tested versions and exact coverage. Bug reports should describe the task, control, browser, and assistive technology involved; a synthetic example is preferred to private source content.

## Source fidelity and citations

The reader presents structured extracted text, not a full facsimile or general-purpose ebook reader. Word headers/footers, complex layout, image text, and unsupported package parts may require consulting the original. There is no PDF ingestion, OCR expansion, DRM removal, or automatic interpretation of illustrations in this release.

Source structure and contributor detection retain uncertainty. A detector supported by one book's byline pattern is not a universal authorship rule. Missing or ambiguous metadata does not make otherwise healthy text unavailable.

Citations are drafts. They do not invent page numbers from extraction ordinals, silently credit anthology editors as authors, or treat a related format's ISBN as proof of edition equivalence. Resolve warnings against the copy you actually used and follow your required citation style.

Saved selections and old passage links rely on identifiable source text. Changed source bytes may leave a correction or selection unresolved. Preserving uncertainty is preferable to transferring it to a nearby passage by guesswork.

## Workflow and integration

- The server is localhost-only, single-user software. There is no hosted deployment, remote sharing, authentication system, or multi-user access control.
- Ordinary ChatGPT has no direct library connector in this release. Codex needs local file/command access; Bookshare acquisition additionally needs a supported signed-in browser workflow.
- Bookshare workflows depend on the site's current UI and the user's account permissions. Manual download followed by local intake is the fallback.
- Intake progress updates require explicit refresh; automatic polling is deferred.
- The synthetic demo is read-only and disposable. It cannot be used to test paid review or editing.
- Metadata review may fail if the configured provider account cannot use the model. The app does not choose another model automatically.
- Changing the automatic-review default affects future imports, not already-authorized jobs. Pause existing jobs explicitly.

Books, private writing, research histories, and original downloads are not part of the public repository. Personal-writing search, general cloud storage sync, and media narration are outside the preview.
