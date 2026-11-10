# Operations and troubleshooting

These commands run from the checkout. They do not require activating `.venv`. The saved checkout binding selects your library unless you explicitly override it.

## Start and stop

Double-click **Reading Room.app** in the checkout folder, or run:

```sh
./bin/library doctor --json
./bin/library app
```

The window shows the same interface and address as the browser version. If no Reading Room server is running, the window starts one and stops it when you close the window or press **Control-C** in its terminal; the authorized intake worker stops with it. If this library's server is already running (for example the login service), the window reuses it and leaves it running. It refuses to open over a different library's server or an unrelated program on the port.

The app's first launch may ask for permission to open files in your Documents folder; choose **Allow**. If it was denied, enable **Reading Room** under System Settings → Privacy & Security → Files and Folders. The launcher records its last run in `.reading-room/app.log` and shows an alert if it cannot start. Setup regenerates the app only when its sources change, which keeps that permission. `.venv/bin/python scripts/build_mac_app.py` rebuilds it manually. The app finds its checkout by location, so it keeps working if you move the whole checkout folder, but not if you move the app out of it.

For a browser tab instead:

```sh
./bin/library serve
```

Open the printed loopback address. Stop a foreground server with **Control-C** in its terminal. Closing the browser does not stop the server or its authorized intake worker.

If another instance uses the default port, inspect it before starting a duplicate. For a deliberately separate manual instance, use `serve --port 18766` or `app --port 18766`; the generated citation links follow that override.

## Start at login

First confirm manual startup works, then stop that foreground server with **Control-C** so the login service can use its port. Install the optional user LaunchAgent:

```sh
./bin/library autostart status --json
./bin/library autostart install --json
```

The agent uses absolute executable and library paths. If paid features are enabled, configure the private `openai_env_file` reference first: a login service does not inherit the key exported in your terminal. See [credentials](configuration.md#credentials).

Installation reports existing service and port conflicts instead of replacing unrelated services. Status reports the managed service and paths for inspection. After moving the checkout, remove and reinstall its managed agent so it does not point to the old location.

To stop and remove this installation's login service:

```sh
./bin/library autostart remove --json
```

This leaves books and library state intact. A pre-existing service from an older installation is not silently removed; inspect the reported conflict and manage that service deliberately.

## Update

1. Make a current backup as described below.
2. Stop the foreground server, or remove this installation's optional login service.
3. Update the checkout and rebuild, supplying the same library location:

   ```sh
   git pull --ff-only
   ./bin/setup --root "/absolute/path/to/your/books" --non-interactive
   ./bin/library doctor --json
   ```

4. Restart manually, verify the shelves and status, then reinstall login startup if you use it.

If Git reports local modifications or divergent history, stop and review them; do not reset away your work. Source releases require rebuilding the frontend after updates. The skill symlinks follow the updated checkout automatically, but Codex may need to refresh discovery.

Read release notes before restoring an older application version against newer private state. Rebuilding an index is not the same as rolling back a state-format migration.

### Continue an older semantic index

New OpenAI semantic indexes use the runtime's `tiktoken` tokenizer and do not need a local language model or PyTorch. Older installations may have an OpenAI index built with the earlier E5 windowing tokenizer. Existing vectors, cached work, and query access remain usable; updating the application does not automatically split the text differently or pay to re-embed it.

To extend or rebuild an older E5-windowed index, install the optional compatibility tokenizer from this checkout:

```sh
.venv/bin/python -m pip install -e '.[legacy-tokenizer]'
```

This adds Transformers and SentencePiece, not PyTorch or a local embedding model. First use may download the pinned public tokenizer assets needed by that index. It does not grant permission to send source text or start a paid build. Keep the old index's recorded tokenizer revision and caches; do not delete them to force the new windowing scheme. Ordinary querying of the existing OpenAI index does not need this compatibility installation.

## Back up and restore

Stop the server before taking a consistent backup. Back up the **whole books directory, including hidden `.library` state**, plus retained downloads/provenance stored elsewhere. Preserve your credential file separately in appropriate private storage. The source checkout alone is not a library backup.

Important durable records include catalog and acquisition identities, intake permissions/results/receipts, curation, saved selections, passage history, research-use events, focuses, taxonomy, review drafts, configuration, and the cost ledger. Paid embedding caches should also be retained: they avoid unnecessary repeat charges.

To restore:

1. Stop the server and keep a copy of the current state.
2. Restore the book directory and its matching `.library` backup together, ideally at their original absolute paths. Durable provenance and source identities can contain paths.
3. Restore or update the credential-file reference without copying a key into configuration.
4. Run `doctor`, then `status`. If necessary, rebuild local indexes:

   ```sh
   ./bin/library sync --rebuild --exact --json
   ```

5. Inspect the result before deliberately resuming paid work.

The exact rebuild preserves original books and durable editorial history. Do not delete `.library` to fix an index: that would discard much more than a cache. Restoring onto different paths can require explicit reconciliation; do not bulk-edit identity/provenance records or assume old links transferred correctly.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `bin/library` cannot find Python | Run setup and check that `.venv/bin/python` exists. A moved or deleted checkout also breaks installed skill links. |
| UI says the frontend is missing | Run `npm --prefix frontend ci` and `npm --prefix frontend run build`, then restart. |
| Library path is missing or wrong | Run `doctor`; check `--root`, `LIBRARY_ROOT`, and `.reading-room/local.json` in that precedence order. |
| Address already in use | Check `autostart status`; stop the intended process or choose another loopback port. Do not kill an unknown process blindly. `library app` reuses a server already running for the same library. |
| Reading Room.app does nothing or shows an alert | Read `.reading-room/app.log`. If the log says “Operation not permitted”, allow **Reading Room** under Privacy & Security → Files and Folders. Rerun `./bin/setup` if the app is missing. |
| Intake remains queued | Keep the Reading Room open (Reading Room.app, `library app`, or `serve`). The development API and demo do not run production intake work. Refresh status explicitly. |
| Text works but semantic results are absent | Check `semantic status`, coverage, authorization, credential readiness, and accepted budget. Enablement alone does not build the index. |
| Review did not run | Check the intake's saved permission, not only the current future-import default. Use `intake review INTAKE_ID` for a deliberate later review. |
| Budget/provider pause persists | Inspect the reason and remaining allowance; fix the cause and explicitly resume. A new month does not automatically resume a paused job. |
| API key works in terminal but not at login | Configure `openai_env_file`; `launchd` does not inherit interactive shell exports. |
| A citation has missing metadata | Read the source and correct/verify the appropriate scope. Reindexing does not establish an unknown author or page number. |
| A skill installer reports a conflict | Preserve the existing skill; inspect its source and choose deliberately. Installation never merges or replaces customized skills. |

Provider access errors do not trigger an automatic model substitution. Exact search, reading, and manual editing remain available. Do not paste full logs into an issue without checking them for paths, excerpts, identifiers, and credentials.

## Uninstall

Remove the managed login service and global skill links first:

```sh
./bin/library autostart remove --json
.venv/bin/python scripts/manage_skills.py uninstall
```

Stop any manually started server. You can then remove the checkout and its `.venv`/frontend dependencies. These commands do not remove books, private library records, original downloads, or credentials. Keep those unless you separately choose to delete them after verifying a backup.
