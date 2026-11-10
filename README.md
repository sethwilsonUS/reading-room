# The Reading Room

A local research library for finding passages, reading them in context, and keeping track of what the sources actually support.

The Reading Room combines a Python indexing service, a browser interface, and Codex skills for finding and downloading authorized Bookshare copies and working with your library. It preserves contributor roles, source locations, uncertain metadata, and your corrections instead of treating every search result as a verified citation.

Browse the complete [skills and their reference guides](skills/README.md) directly in the repository. Setup links them for Codex to discover in this project.

**v0.1.0 is a macOS technical preview.** You install it from source. Books and private library records stay outside this repository. No books, Bookshare membership, Codex access, or OpenAI API usage are included.

## Try it

You need Git, **Python 3.12–3.15**, and a supported Node.js version; **Node 24.15.0 or newer within 24.x** is recommended. Node is needed to install/build the interface; normal operation uses one Python server. See the [quickstart](docs/quickstart.md) for the supported version ranges and expected results.

```sh
git clone https://github.com/sethwilsonUS/reading-room.git
cd reading-room
./bin/setup
./bin/library demo
```

Open the address printed by the demo, normally [http://127.0.0.1:18768](http://127.0.0.1:18768). Search for **copper birds**. The demo uses invented text in a temporary library, with paid processing disabled. Press Control-C in the terminal to stop it.

To open your own library afterward, double-click **Reading Room.app** in the checkout folder (setup creates it), or run:

```sh
./bin/library app
```

The library opens in its own window; closing the window stops the server it started. The first launch may ask for permission to open files in your Documents folder. Prefer a browser tab? `./bin/library serve` serves the same interface at [http://127.0.0.1:18765](http://127.0.0.1:18765). Follow [your first book](docs/quickstart.md#add-your-first-book) to add a permitted EPUB or DOCX. The server listens on your computer only.

## What works locally

- Exact-text search, shelves, structured reading, and book contents.
- Chicago notes-and-bibliography citation drafts, with missing or uncertain information marked for review.
- An agent-friendly CLI: `library search QUERY --with-context --json` returns the interface's results with surrounding source text and citation drafts.
- Editable metadata, research focuses, classifications, and separate research-use history.
- Durable source selections and corrections that survive index rebuilds.

Optional OpenAI features add **semantic search** and **metadata review**. They have separate opt-ins and share configured spending controls. A fresh installation makes no paid requests, even if an API key is already in your environment. [Understand AI, privacy, and costs](docs/ai-and-privacy.md) before enabling either feature.

## Choose a guide

| I want to… | Start here |
| --- | --- |
| Install and try the app | [Quickstart](docs/quickstart.md) |
| Use Codex with my library or Bookshare | [Codex and skills](docs/codex.md) |
| Add a local book or accessible export | [Acquisition and intake](docs/intake.md) |
| Search, read, cite, and correct information | [Everyday use](docs/using-the-library.md) |
| Set paths, keys, limits, or ports | [Configuration reference](docs/configuration.md) |
| Understand optional paid processing | [AI, privacy, and costs](docs/ai-and-privacy.md) |
| Start at login, update, back up, or troubleshoot | [Operations](docs/operations.md) |
| Change the code or contribute | [Development](docs/development.md) and [Contributing](CONTRIBUTING.md) |
| Check support and accessibility boundaries | [Known limitations](docs/limitations.md) |

## Project status

This is a personal open-source project by Seth Wilson. Issues and contributions are welcome; response times and ongoing support are best effort. The supported preview is macOS, source installation, and Codex-assisted local workflows. Other operating systems, hosted access, and a direct ordinary-ChatGPT connection are outside this release.

Keyboard and screen-reader access matter here. Exact quotation selection in the UI currently depends on mouse highlighting; passage and whole-section citation routes are available separately. Read the [accessibility limitations](docs/limitations.md#accessibility) rather than assuming a comprehensive conformance claim.

Original code, skills, and documentation use the [MIT license](LICENSE). Books remain subject to their own rights. Third-party components retain their own licenses; see [dependency and asset notices](NOTICE.md). The project is independent of Bookshare and OpenAI.
