# Configuration reference

There are two configuration locations. The checkout's ignored `.reading-room/local.json` remembers the chosen library directory for its launcher. The library's private `.library/config.json` holds its settings and references to credentials. Neither belongs in Git.

Use supported commands for paid-feature settings rather than rewriting the configuration file. They validate values and preserve unrelated settings, authorizations, and accounting history.

## Paths and precedence

For `bin/library` and the bundled skill launcher, library-root precedence is:

1. `--root PATH` before the subcommand.
2. `LIBRARY_ROOT` in the process environment.
3. `library_root` in the checkout's `.reading-room/local.json`.
4. The current user's `~/Documents/Reading Room`.

```sh
./bin/library --root "$HOME/Books/OtherLibrary" status --json
```

A configured missing directory is an error for ordinary commands; it does not silently select another collection. Use `init` to initialize an intended new root. The direct Python `Library` interface accepts an explicit root; code outside the launcher should pass it or set `LIBRARY_ROOT` rather than assume a checkout binding.

## Environment variables

| Variable | Purpose and default |
| --- | --- |
| `LIBRARY_ROOT` | Book directory. Overrides the checkout binding; `--root` takes priority. |
| `OPENAI_API_KEY` | Optional server-side API key. If nonempty, takes precedence over credential files. It does not enable paid features. |
| `LIBRARY_OPENAI_ENV` | Path to a private environment file containing `OPENAI_API_KEY`. Used when the direct key is absent; overrides `openai_env_file`. |

There are no required application environment variables for the local quickstart and no frontend API-key variables. The root `.env.example` is documentation, not a file the application automatically sources. Do not put secrets in `frontend/.env` or `VITE_*` variables; frontend values can become browser code.

Development commands inherit `LIBRARY_ROOT`. Set it explicitly to a disposable library before running the Vite API companion, which does not rely on a shell's activated environment or infer your intended test collection.

## Credentials

Create a private file outside the checkout containing one assignment:

```dotenv
OPENAI_API_KEY=replace-with-your-own-key
```

The example is a placeholder, not a usable key. Restrict the actual file to your user account, then reference its absolute path with `LIBRARY_OPENAI_ENV` or `openai_env_file` in `.library/config.json`. For example, this is a **merge fragment**, not a replacement for an existing configuration:

```json
{
  "openai_env_file": "/absolute/private/path/reading-room.env"
}
```

Keep existing configuration fields when adding it. The key itself stays in the private file, not the JSON. The loader reads a value; it does not execute the file as a shell script. Diagnostics report credential readiness/source without printing the secret.

Credential precedence is direct `OPENAI_API_KEY` → file named by `LIBRARY_OPENAI_ENV` → configured `openai_env_file`. If a selected credential file is wrong or unreadable, fix that reference rather than expecting a different key source to mask it.

## Supported settings

| Setting | Meaning / how to change it |
| --- | --- |
| `local_base_url` | HTTP loopback address, default `http://127.0.0.1:18765`. Controls listener and citation links. |
| `openai_env_file` | Optional absolute credential-file reference; no key value. |
| `budget.initial_usd` | Accepted limit for the initial full-library embedding build; use `budget configure`. |
| `budget.monthly_usd` | Accepted shared monthly limit; use `budget configure`. |
| `budget.timezone` | IANA timezone for calendar-month accounting. New initialization detects the system zone when possible, otherwise UTC; review or choose it explicitly at opt-in. |
| `semantic.enabled` and `semantic.engine` | Semantic authorization and selected engine; use `semantic enable` / `disable`. Public setup supports OpenAI. |
| `metadata_review.enabled` | Remembered automatic-review default for future intake; use `review enable` / `disable`. |

The same file also contains schema/version and authorization bookkeeping, including approval evidence/times, accepted-budget records, and semantic initial-build completion. Preserve these fields. They are not a reset switch for spending or a shortcut to authorization.

Fresh paid settings are off. The $10/$10 suggestions become accepted limits only through explicit setup. Older libraries preserve their existing $50 initial / $25 monthly amounts and Chicago accounting unless deliberately changed; their past cost records are not rewritten.

## Port and address

Temporarily override the configured port:

```sh
./bin/library serve --port 18766
```

This process uses matching citation links without changing the saved address. To change the saved address, merge an appropriate `local_base_url` value into the private configuration and restart the service. Only HTTP loopback hosts (`127.0.0.1`, `localhost`, or `::1`) with a valid port and no path or credentials are accepted.

The application is not a remotely hosted or authenticated multi-user service. Binding publicly, tunneling it, or reverse-proxying it is outside the supported configuration.

## Useful checks

```sh
./bin/library doctor --json
./bin/library status --json
./bin/library budget status --json
./bin/library semantic status --json
./bin/library review status --json
```

Doctor checks installation/configuration. Library status checks source freshness and indexing/metadata coverage. A configured credential is not proof that the provider account can use a model; errors leave local features available.
