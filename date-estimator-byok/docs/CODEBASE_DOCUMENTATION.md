# Date Estimator BYOK: Code Overview

## Local architecture

`run.py` creates or reuses `venv/`, installs `requirements.txt`, launches `app.py` on a loopback port, and opens the browser. Set `PORT` to choose a fixed port. The app rejects non-loopback requests and does not provide a hosted service, app accounts, central key storage, telemetry, replay, or dataset collection endpoints.

| Path | Responsibility |
| --- | --- |
| `app.py` | Flask routes, spreadsheet processing, provider requests, image workflows, local settings, and optional Microsoft OAuth |
| `imslp_estimator/core.py` | Publisher retrieval and matching, plate-table lookup, date estimation, and result formatting |
| `imslp_estimator/date_range.py` | Date ranges and range combination |
| `imslp_estimator/address_range.py` | Address extraction, normalization, and matching |
| `imslp_estimator/publisher_mapping.py` | Canonical publisher aliases |
| `imslp_estimator/imprint_mapping.py` | Imprint-to-publisher mappings |
| `imslp_estimator/data/` | Local mapping data |
| `templates/` | Jinja pages for the local workflows |
| `static/` | Local stylesheet and favicon |
| `tests/` | Automated regression tests |

## Date estimation and spreadsheets

`estimate_year()` normalizes publisher and plate input, matches an IMSLP publisher or imprint, retrieves publisher information, and combines compatible publisher, imprint, address, and plate-number date ranges. Its explanation and source information accompany the estimate.

`/search` handles a single estimate. `/upload` accepts an Excel workbook with `City`, `Publisher name`, `Address`, and `Plate number` columns. Preview and review pages expose the resulting estimates; downloads provide simple, detailed, and cataloging workbooks. Research routes expose publisher matching, similarity, imprint lookup, address lookup, plate tables, and formula generation.

Publisher lookups use the network. The local application is not an offline copy of all IMSLP publisher information.

## Providers and settings

`API_PROVIDERS` in `app.py` defines provider labels, model choices, default models, and environment-key names. Supported implementations are Harvard OpenAI, Harvard Gemini, OpenAI, Anthropic, and Google Gemini. Harvard Gemini shares the Harvard OpenAI key.

`/settings` accepts POST actions `save_key`, `remove_key`, `set_active`, and `set_model`. The template uses `providers`, `masked_keys`, `active_provider`, and `selected_models`. Optional environment keys can supply a fallback without an unlock flow.

Keys entered through Settings are encrypted using the local `FLASK_SECRET_KEY` and stored in the filesystem session. The secret is generated in `.env` when missing. Sessions expire after 30 days; keys and preferences are not synchronized to other computers.

## Image workflows

`/image-transcription` accepts uploads, local image paths, or optional Microsoft sources. The selected provider receives the image and transcription prompt; the returned fields can be reviewed and exported.

`/batch-transcription` manages a source folder or uploaded set, prompt, and current run. Image listing, transcription, and `/add-to-run` support review. Accepted rows are saved under `batch_results/<run_id>.json`; appends use locks and atomic writes. Original predictions and field correction provenance remain local run metadata. `/export-run/csv` and `/export-run/xlsx` export accepted rows.

`/model-comparison` lists local images and available Harvard models. `/model-comparison/run` applies the chosen prompt to each selected image/model pair and returns side-by-side results. It uses the caller's Harvard key, and each pair can incur a provider charge.

Local operation does not guarantee zero data retention. Image content and prompts leave the computer for provider calls. Comparison can send the same image to multiple selected providers. Provider privacy, retention, and usage policies apply.

## Optional Microsoft storage

OneDrive and SharePoint routes use the caller's Microsoft OAuth configuration: `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, and `MICROSOFT_TENANT_ID`. Tokens are kept in the local session. These connections are optional; uploads and local folders do not depend on them.

Use a fixed `PORT` and register the matching loopback `/oauth/onedrive/callback` URI. Review permissions and tenant restrictions in your own Microsoft application registration. The app's Microsoft connection is separate from model provider keys.

## Local state and checks

Keep `.env`, `flask_session/`, `uploads/`, `batch_results/`, and `logs/` private. They can contain secrets, source images, bibliographic data, and diagnostics. Do not deploy this local edition as a public server.

After installing dependencies and activating the virtual environment:

```bash
python -m pip install pytest
python -m py_compile app.py run.py
python -m pytest tests/ -q
```

Use Python 3.11 or 3.12. Keep paid provider calls and live network checks separate from deterministic tests. For user-facing setup and operation, see the README or the in-app User Guide.
