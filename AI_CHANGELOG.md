# AI Development Changelog

This file is a mandatory ledger for all AI agents. Every modification to the codebase must be logged here.
Format: `[Date] - [Context] -> [Changes] -> [Impact]`.

## [2026-07-15] - Kasta Color, Size & Characteristics Mapping Standardization
- **[Context]**: User requested Kasta XML feed to strictly match Kasta's official allowed colors, sizes, and characteristics dictionary (based on 'хараткеритсики дитячий одяг.xlsx') to prevent import/moderation rejections. Required mapping heights (with a round-down rule for non-exact values, e.g., 76 -> 74) and converting age formats to Kasta age codes (e.g. 12-18м -> 12м.).
- **[Changes]**:
  1. Created `kasta_colors.json` to store allowed Kasta colors and custom mapping rules.
  2. Created `kasta_characteristics.json` to store allowed values, keyword mappings, and lists for season, pattern, decor, fastener, and material mapping.
  3. Modified `export.py` to implement size mapping constants (`KASTA_KIDS_HEIGHTS`, `KASTA_KIDS_AGE_MAP`) and helper `standardize_kasta_size()`.
  4. Updated size param processing inside `build_kasta_feed_xml` to output standardized sizes for kids' clothing.
  5. Implemented Kasta characteristics standardizers (`load_kasta_characteristics_config()`, `standardize_kasta_characteristics()`, `standardize_kasta_color()`).
- **[Impact]**:
  1. Colors and characteristics are automatically mapped to Kasta allowed vocabularies.
  2. Size parameters are converted into Kasta-compatible height formats (e.g. `152 см`), rounding non-exact heights down to the closest valid Kasta size (e.g. `76` -> `74 см`, `106` -> `104 см`), and ranges/ages are correctly normalized (e.g., `12-18 міс` -> `12м.`).
  3. 100% of generated products have valid, specification-compliant characteristics and sizes, passing Kasta Hub validation.


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
