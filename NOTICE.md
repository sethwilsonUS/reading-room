# Attribution and third-party notices

The Reading Room's original code, agent skills, documentation, SVG mark, and synthetic demonstration text are distributed under the MIT license. Copyright © 2026 Seth Wilson. The geometric SVG mark uses no external imagery; the UI uses system fonts.

Books, acquired source files, and personal research are not included and are not licensed by this project. Synthetic tests may use real publication or contributor names to exercise attribution conventions; their test prose is invented.

Dependencies retain their own licenses. This source release does not relicense them. The generated browser build includes [browser runtime notices](frontend/public/third-party-notices.txt). [Python runtime notices](docs/licenses/python-runtime.txt) accompany this inventory. Development tools are installed separately; their license metadata and notices are in their npm/Python distributions.

Regenerate this inventory after installing the locked dependencies with `.venv/bin/python scripts/dependency_notices.py`, before building the UI. The tables describe the locked release inputs, not every optional local-model dependency.

## Python runtime

| Dependency | Version | License |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| bottle | 0.13.4 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| distro | 1.9.0 | Apache License, Version 2.0 |
| fastapi | 0.162.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jiter | 0.18.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| numkong | 7.8.6 | Apache-2.0 |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| openai | 2.54.0 | Apache-2.0 |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| pillow | 13.0.0 | MIT-CMU |
| proxy_tools | 0.1.0 | MIT |
| pydantic | 2.14.2 | MIT |
| pydantic-core | 2.50.2 | MIT |
| pyobjc-core | 13.0 | MIT |
| pyobjc-framework-Cocoa | 13.0 | MIT |
| pyobjc-framework-Quartz | 13.0 | MIT |
| pyobjc-framework-Security | 13.0 | MIT |
| pyobjc-framework-UniformTypeIdentifiers | 13.0 | MIT |
| pyobjc-framework-WebKit | 13.0 | MIT |
| pywebview | 6.2.1 | BSD License |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache-2.0 |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| starlette | 1.10.0 | BSD-3-Clause |
| tiktoken | 0.14.0 | See installed license notices |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| usearch | 2.26.6 | Apache-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |

## Browser runtime

| Dependency | Version | License |
| --- | --- | --- |
| @internationalized/date | 3.12.4 | Apache-2.0 |
| @internationalized/number | 3.6.9 | Apache-2.0 |
| @internationalized/string | 3.2.10 | Apache-2.0 |
| @react-types/shared | 3.37.0 | Apache-2.0 |
| @swc/helpers | 0.5.24 | Apache-2.0 |
| @tanstack/query-core | 5.107.1 | MIT |
| @tanstack/react-query | 5.107.1 | MIT |
| @types/react | 19.3.0 | MIT |
| aria-hidden | 1.2.6 | MIT |
| client-only | 0.0.1 | MIT |
| clsx | 2.1.1 | MIT |
| cookie | 1.1.1 | MIT |
| csstype | 3.2.3 | MIT |
| react | 19.3.1 | MIT |
| react-aria | 3.53.1 | Apache-2.0 |
| react-aria-components | 1.22.1 | Apache-2.0 |
| react-dom | 19.3.1 | MIT |
| react-router | 7.18.5 | MIT |
| react-router-dom | 7.18.5 | MIT |
| react-stately | 3.51.0 | Apache-2.0 |
| scheduler | 0.28.0 | MIT |
| set-cookie-parser | 2.7.3 | MIT |
| tslib | 2.9.1 | 0BSD |
| use-sync-external-store | 1.7.0 | MIT |
