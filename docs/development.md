# Development

The supported development environment is macOS, Python 3.12–3.15, and Node.js 24.15.0 or newer within 24.x (recommended). The locked frontend also permits 22.22.2+ within 22.x or 26.0.0 and newer, matching the package engine range. Start with [setup](quickstart.md), then install the locked development dependencies using the commands below.

## Architecture

```text
Codex skills / library CLI / Reading Room UI
                    |
              Python service
                    |
 acquisition → extraction → source structure → exact index
                    |                 |
           durable corrections   optional embeddings
                    |
            reading and citations

 optional review → proposals / eligible provisional corrections
```

`src/book_library` owns the service, storage, extraction, attribution, citations, intake worker, and paid-request accounting. `frontend` owns the React/TypeScript interface. FastAPI serves the built assets and typed page APIs on loopback. Skills orchestrate acquisition and research through the same service boundary rather than maintaining another index.

Original book packages are immutable inputs. Durable records under the selected library's `.library` are separate from derived text/index generations. A writer lock serializes publication of durable changes; completed index generations publish atomically. Review proposals, saved corrections, personal verification, and usage history have distinct meanings. Read the [glossary](../GLOSSARY.md) and [architectural decisions](adr/0001-source-structure-overlay.md) before changing those relationships.

The complete skill sources live in [`skills/`](../skills/README.md). Setup creates ignored relative links under `.agents/skills` for Codex discovery. Edit the originals; keep generated discovery links out of Git and source archives.

## Install development dependencies

```sh
.venv/bin/python -m pip install -r requirements-dev.lock.txt
```

The normal lock excludes the experimental local embedding stack. Historical pilot code remains for compatibility, outside the supported setup. Do not run a benchmark or pilot against a personal library as a test fixture.

## Run checks

```sh
.venv/bin/python -m pytest -q
npm --prefix frontend run types
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend exec -- playwright install chromium
npm --prefix frontend run test:browser
.venv/bin/python scripts/check_docs.py
.venv/bin/python scripts/check_public_release.py --candidates
```

Python tests construct temporary synthetic EPUB/DOCX sources and mock providers. Pytest treats `ResourceWarning` as an error: open SQLite through `book_library.storage.open_sqlite` (or `connect` for read-only index generations) inside a `with` block, which closes the connection. The standard `sqlite3.connect` context manager only commits. Browser tests run a disposable server on port 18767 with paid work and the production intake worker disabled. They must not use real books or keys. Automated accessibility checks supplement, rather than replace, keyboard and screen-reader review.

`library app` (`src/book_library/desktop.py`) wraps the same server in a pywebview window; see [ADR 0008](adr/0008-native-desktop-window.md). `Reading Room.app` is generated from `macos/` by `scripts/build_mac_app.py` during setup and is not committed. Rebuild it with `--force` after editing those sources if setup has not run.

API type generation is offline: it exports the FastAPI schema without opening a real library or starting the worker. Commit the generated OpenAPI/TypeScript contracts with relevant API changes. Frontend build output under `src/book_library/frontend_dist` is generated locally and excluded from Git.

## Run the development servers

Stop the production server or remove the managed login agent first; Vite needs port 18765. Select a disposable library explicitly. In one terminal:

```sh
LIBRARY_ROOT="/absolute/path/to/disposable-library" npm --prefix frontend run dev:api
```

In another:

```sh
npm --prefix frontend run dev
```

Open [http://127.0.0.1:18765](http://127.0.0.1:18765). Vite proxies to a worker-disabled API on 18766 while preserving the public address and request-origin checks. Do not substitute `library serve --port 18766`: that starts the production intake worker and changes generated links.

Stop both processes, rebuild, and restart your normal service after frontend changes.

## CLI and HTTP interfaces

`library --help` and each subcommand's `--help` document accepted arguments. `--root` precedes the command; `--json` is available on service commands for agent use. Prefer service methods and CLI JSON over reading private JSONL or SQLite internals directly.

The `/api/ui/` routes provide typed UI page data; existing `/api/` reading and citation contracts remain supported. Read-only GET routes must not enqueue work or make paid calls. Mutations use protected JSON actions; preserve loopback Host/Origin and CSRF checks. HTML/XML extracted from a book is untrusted data: escape it and never execute scripts or load source network resources.

Search submissions and paid reviews are explicit. Navigation, browser history restoration, automatic query retries, focus changes, and reconnects must not replay paid operations. Local review-draft autosave is distinct from applying a correction.

## Publication boundary

The repository and source package use explicit inclusion rules. Keep book files, retained downloads, `.library`, `.catalog`, `.reading-room`, credentials, reports, `_work`, and personal curation scripts out of both Git and packages. `.gitignore` alone does not prove a package is safe.

Run the repository's documentation/public-content checks before release, and inspect the actual archive contents. `--candidates` checks the working-tree allowlist before staging. With no flag, the check reads the **Git index's staged blob contents and file modes**, not the current working-tree files: stage the intended release first and check again after any staged changes. Unresolved entries and non-regular files are rejected. Use `--archive /absolute/path/to/archive` to inspect a built source package or wheel. Synthetic fixtures must be clearly labeled; never make a real book excerpt into a test or screenshot for convenience. See [release procedure](release.md) and [contributing](../CONTRIBUTING.md).
