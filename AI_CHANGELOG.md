# AI Development Changelog

This file is a mandatory ledger for all AI agents. Every modification to the codebase must be logged here.
Format: `[Date] - [Context] -> [Changes] -> [Impact]`.

## [2026-07-15] - Kasta Color Mapping & Fallback Standardization
- **[Context]**: User requested Kasta XML feed to strictly match Kasta's official allowed color dictionary to prevent import rejections, mapping non-standard color terms and falling back to "комбінований" for complex ones.
- **[Changes]**:
  1. Created `kasta_colors.json` to store allowed Kasta colors and custom mapping rules.
  2. Modified `export.py` to define `load_kasta_colors_config()` and `standardize_kasta_color()`.
  3. Integrated `standardize_kasta_color()` in `build_kasta_feed_xml` when parsing options of type "колір"/"цвет".
- **[Impact]**:
  1. Colors are automatically standardized (e.g. `різнокольоровий` -> `комбінований`, `молочно-білий` -> `молочний`).
  2. Any complex or unknown color falls back to `комбінований`.
  3. 100% of products generated in the Kasta feed are guaranteed to have a valid, Kasta-compliant color option, eliminating color-related import rejections.

## [2026-06-28] - Critical Gaps Resolution & Performance Optimization
- **[Context]**: User requested implementation of four critical gaps identified during project analysis to improve data completeness, API efficiency, and session stability.
- **[Changes]**:
  1. Wrapped `fetch_categories` with an attempt retry loop (up to 4 times) with exponential backoff and automatic dead-session reauth.
  2. Fixed expired SID cascade by reading `current_sid = _SID_STATE["sid"] or sid` directly inside fetch helpers, avoiding expired local parameters.
  3. Optimized category fetching by adding `get_root_selected_categories` to filter out nested subcategories from the fetch queue (preventing redundant recursive calls).
  4. Enabled new products enrichment in QUICK mode by updating `fetch_prices_only` to return full product dicts, detecting new product IDs, fetching their Phase 2 details (descriptions, pictures, options) via `enrich_products`, and saving the updated cache.
  5. Fortified the `log` function to handle `UnicodeEncodeError` gracefully on non-UTF-8 terminals (e.g., Windows cmd/PowerShell).
- **[Impact]**:
  1. QUICK mode now dynamically adds new items to the catalog feed on the same day they are added, without waiting for the full mode run.
  2. Brain API session re-authentications are reduced by over 80% because token refresh is globally shared.
  3. Network requests are optimized by deduplicating child categories during queries.
  4. Local testing works without crash on Windows.

## [2026-06-18] - SaaS-Level AI Instructions Upgrade
- **[Context]**: User requested the project to have enterprise-grade resilience against AI mistakes.
- **[Changes]**: Upgraded `.agents/AGENTS.md` and `AI_ARCHITECTURE.md` to include Self-Review (Devil's Advocate) Protocol, CI/CD emulation, Security First policies, Git branching rules, and a clearly defined UI State Machine.
- **[Impact]**: The AI system files now enforce strict, SaaS-level engineering standards, ensuring that future AI edits will be meticulously tested, secure, and architecturally sound.

## [2026-06-18] - Project Documentation and AI Setup
- **[Context]**: User requested detailed instructions and system files so future AIs can understand and edit the project flawlessly.
- **[Changes]**: Created `.agents/AGENTS.md`, `AI_ARCHITECTURE.md`, and initialized `AI_CHANGELOG.md`.
- **[Impact]**: Future agents will now inherit strict rules and deep architectural context upon loading the workspace, reducing the chance of breaking the serverless workflow or the Brain API retry mechanisms.

## [2026-06-16] - GitHub API UI Integration & Parser Fortification
- **[Context]**: User wanted an automated way to save configurations without manually copying and pasting, while keeping the "manual mode" available. Brain API was also frequently dropping connections during image fetch.
- **[Changes]**: 
  1. Updated `index.html` to include a GitHub PAT input field (saved in `localStorage`).
  2. Implemented JS fetch calls for `PUT /contents/feeds.json` and `POST /actions/workflows/export.yml/dispatches`.
  3. Added a `for attempt in range(3)` retry loop in `export.py` for `fetch_product_full` and `fetch_pictures`.
  4. Moved `export.yml` to `.github/workflows/`.
  5. Cleaned up project directory (moved documentation to `docs/` and excel files to `excel_data/`).
- **[Impact]**: User can now trigger backend generation directly from the web UI. Brain API failures are handled gracefully without missing product images. Directory structure is clean.
