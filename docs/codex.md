# Codex and skills

The full agent workflow uses Codex with access to this local checkout and your chosen books folder. Reading Room itself does not require Codex: its window, browser interface, and CLI work independently.

There are three separate sign-ins or credentials:

| Credential | Purpose |
| --- | --- |
| Codex sign-in | Run the assistant that uses the skills and local tools. |
| Bookshare sign-in | Search/download the accessible copies your account can obtain. |
| OpenAI API key | Optional Reading Room semantic search and metadata review, billed through the API account. |

Signing into Codex does not configure an API key in Reading Room. Signing into Bookshare does not enable paid processing.

## Use the skills in this project

1. Complete the [quickstart](quickstart.md).
2. Install or open the desktop app that provides Codex and sign in. The current [official desktop getting-started guide](https://learn.chatgpt.com/docs/app) describes installing ChatGPT, then choosing **Codex**; existing releases may use a Codex-branded app.
3. Open the Reading Room checkout as a local Codex project.
4. Allow the file access needed for this checkout and your books directory when Codex asks.
5. Look for `bookshare-find`, `bookshare-download`, and `library-search` in the skill picker. Restart or refresh skill discovery if newly added skills are not visible.

The complete, browsable originals live in [`skills/`](../skills/README.md). Each includes its reference material; the download skill also includes a package-inspection helper. Setup creates relative links under `.agents/skills` for project discovery, without installing user-wide skills. Their launcher finds this checkout's Python environment and saved library path without requiring shell activation. Codex's [official skill documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills) describes project and user discovery, including symlinked skill folders.

To inspect or recreate just the project links, without rerunning dependency installation:

```sh
.venv/bin/python scripts/manage_skills.py status --project
.venv/bin/python scripts/manage_skills.py install --project
```

Project links are generated local state, excluded from Git and source packages. Existing customized project skills are reported as conflicts and preserved. Edit the originals in `skills/` when contributing changes.

Try a prompt such as:

> Use library-search to check library status, then find passages about memory and place. Read the strongest results in context and distinguish direct support from possible connections. Do not enable paid features.

If semantic search is already enabled, ordinary search may make a paid query embedding. Ask explicitly for **exact search** when you want a wholly local search.

Agents can get each result's surrounding source text and citation draft in one call with `library search QUERY --with-context --json`, prepare metadata corrections with `library correction`, and browse the shelves with `library browse`; see the [command reference](../skills/library-search/references/commands.md).

Other assistants with terminal access to this checkout can follow the same `SKILL.md` files and run the same `bin/library` commands. Setup creates discovery links only for Codex (`.agents/skills`); point another assistant at `skills/` yourself.

## Use the skills across projects

From this checkout:

```sh
.venv/bin/python scripts/manage_skills.py status
.venv/bin/python scripts/manage_skills.py install
```

The installer uses sibling symlinks in `~/.agents/skills` by default. It checks the whole bundle before changing anything, including older user-skill names under `~/.codex/skills`. Existing files, customized skills, or links to another checkout are conflicts, not permission to overwrite them. Resolve the reported conflict deliberately; do not delete a personal skill just to make installation succeed.

For an explicitly chosen skill directory, add `--destination /absolute/path/to/skills`. Avoid installing duplicate copies under different user skill roots. The installer cannot choose which customized skill you want Codex to use.

Updating this checkout updates its linked skills. When updating from the initial preview layout, rerun setup (or `install --project`) first; it restores discovery paths used by older user-level links. After moving the checkout, reinstall the user-level links deliberately. Project links use relative paths and move with the checkout. To remove only this installation's managed user-level links:

```sh
.venv/bin/python scripts/manage_skills.py uninstall
```

Uninstall leaves the checkout, books, private records, and unrelated skills intact.

## Bookshare workflow

Bookshare acquisition uses normal signed-in browser controls. The skills are instructions and local inspection tools, not a Bookshare API client or an independent downloader.

Use the desktop app's supported browser control and sign into Bookshare yourself. The built-in browser has its own profile; an existing sign-in in another browser may not carry over. Browser permissions and UI labels can vary by app version. Follow the [official Browser guide](https://learn.chatgpt.com/docs/browser) or, for an existing Chrome profile, the [Chrome extension setup](https://learn.chatgpt.com/docs/chrome-extension#set-up-the-chrome-extension).

Example prompts:

> Use bookshare-find to find accessible editions of this title. Compare the catalog details with my local holdings and give me a shortlist before downloading.

> Use bookshare-download to download the edition I selected. Retain the original, inspect the actual files, and intake a byte-identical copy into my configured library using local-only processing.

The download skill inspects the actual edition, records discrepancies, and supplies source-neutral intake context. It does not request your password, extract browser credentials, bypass access restrictions, or assume a connector exists. A Bookshare account is needed for this acquisition route, not for using local books from other sources.

If agent browser access or download handling is unavailable, download manually through Bookshare and follow [manual intake](intake.md#manual-intake). Local research remains available through the CLI.

## Codex, ChatGPT, and the local server

This preview supports a local Codex workflow with filesystem and command access. Current OpenAI documentation may describe Codex within the ChatGPT desktop app; labels differ between releases. A normal ChatGPT conversation does not gain access to your localhost library merely because you share a URL.

There is no Reading Room ChatGPT connector, remote MCP service, or hosted endpoint in this release. Do not expose the server publicly to work around that boundary. You can manually copy a citation or a permitted excerpt into another tool, but that is a separate data-sharing action.
