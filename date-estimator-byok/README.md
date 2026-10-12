# Date Estimator BYOK

A local application for estimating music score publication dates from IMSLP publisher records, plate numbers, imprint history, and address clues. Image transcription uses your own API keys. No app account or central service is required.

## Launch locally

Use Python **3.11 or 3.12**. With Git:

```bash
git clone https://github.com/TSASC/cataloging-sheet-music-with-ai.git
cd cataloging-sheet-music-with-ai/date-estimator-byok
python3 run.py
```

Alternatively, [download the repository ZIP](https://github.com/TSASC/cataloging-sheet-music-with-ai/archive/refs/heads/main.zip), extract it, open a terminal in the `date-estimator-byok` subfolder, and run `python3 run.py`. On Windows, use `py -3.12 run.py` if `python3` is unavailable. The `Start.command` (Mac) and `Start.bat` (Windows) launchers are also inside that subfolder.

The launcher creates a virtual environment, installs dependencies, and opens a loopback-only Flask app in your browser. Keep the terminal open while using it; press `Ctrl+C` to stop. Run the same command to reopen it. Installation and online lookups require internet access.

## Workflows

- **Date Search:** estimate a date from publisher, city, address, and plate-number clues.
- **Excel Upload:** process `.xlsx` files containing `City`, `Publisher name`, `Address`, and `Plate number` columns. Review and export simple, detailed, or cataloging spreadsheets.
- **Image Transcription:** extract bibliographic fields from an uploaded image or local file.
- **Batch Transcription:** select uploads or a local folder, review transcriptions, and export accepted rows as CSV or XLSX.
- **Model Comparison:** compare selected Harvard OpenAI and Harvard Gemini models on local images.
- **Research tools:** inspect publisher matches, imprint and address ranges, plate tables, and date formulas.

Review model output and estimated dates before relying on them. The estimator retrieves publisher information from IMSLP and combines available date ranges; an estimate is not an authoritative publication date.

## Bring your own key

Open **Settings**, save a key, select the active provider, and save a model choice. Date search and spreadsheet estimation do not require a model key.

| Provider | Optional local environment variable |
| --- | --- |
| Harvard OpenAI / Harvard Gemini | `OPENAI_API_KEY` |
| OpenAI | `OPENAI_STANDARD_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Google Gemini | `GEMINI_API_KEY` |

Harvard providers require your own authorized endpoint access and share one key. Model Comparison uses that Harvard key. Provider model access, quotas, and charges depend on your account.

Keys entered in Settings are encrypted in a local server-side session using `FLASK_SECRET_KEY`, which is generated in the local `.env` if absent. Sessions expire after 30 days. Your own environment keys are optional fallbacks; removing a saved key does not remove an environment key. There are no shared system keys or unlock passwords.

**Local does not mean zero data retention.** Transcription sends image content and prompts to the selected provider. Comparison sends them to the providers selected for comparison. Provider retention and usage policies apply. Do not send sensitive material without authorization.

The edition has no application telemetry or session replay. Local files still include sensitive information: protect `.env`, `flask_session/`, `uploads/`, `batch_results/`, and `logs/`. Do not publish them or expose the app to other computers.

## Optional OneDrive and SharePoint

Local files work without Microsoft configuration. To use Microsoft sources, supply your own OAuth application configuration in `.env` or your environment:

```text
MICROSOFT_CLIENT_ID
MICROSOFT_CLIENT_SECRET
MICROSOFT_TENANT_ID
```

Use a fixed port so the redirect URI matches your app registration:

```bash
PORT=5001 python3 run.py
```

On Windows Command Prompt, run `set PORT=5001`, then `py -3.12 run.py`.

Register `http://127.0.0.1:5001/oauth/onedrive/callback` as the redirect URI, matching the host and port you open. Connect from the image source selector and review the requested Microsoft permissions. Available accounts, sites, and folders depend on your tenant configuration. Do not bypass browser security warnings.

## Updates and development

Updates are manual. Stop the app, run `git pull --ff-only` in a Git clone, then restart. If you have local code changes, review them before updating. ZIP installations need a new ZIP; preserve local settings and results separately.

After dependencies are installed, activate the virtual environment and run:

```bash
python -m pip install pytest
python -m py_compile app.py run.py
python -m pytest tests/ -q
```

Live provider calls require your keys and can incur charges. Keep them separate from deterministic checks.

## Documentation

- In-app User Guide: `/user-guide`
- In-app technical reference: `/codebase_documentation`
- Code overview: [docs/CODEBASE_DOCUMENTATION.md](docs/CODEBASE_DOCUMENTATION.md)

## License

You're welcome to use and modify this software for yourself or your organization's internal work, and to share copies for free with the license attached. Selling it, selling modified or repackaged versions, or including its code in a product sold to others requires prior written permission, as does offering it as a hosted or managed product. A short note about how you use it is appreciated, but optional. This is source-available software, not open-source software. See [LICENSE](LICENSE) for the complete terms.

This license applies to the Date Estimator application in this folder. It does not relicense the existing Colab notebook or documentation elsewhere in the TSASC repository.
