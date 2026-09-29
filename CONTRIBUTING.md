# Contributing to API Printer Service

Thank you for considering contributing! 🎉

## Code of Conduct

By participating in this project you agree to abide by the
[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). Please report
unacceptable behaviour to the repository owner.

## How can I contribute?

### Reporting bugs

- Open a [Bug Report](.github/ISSUE_TEMPLATE/bug_report.yml) and fill in the
  affected areas checklist.
- Include the service version (visible at `http://localhost:5058/health`).
- Attach the relevant section of the service log (from the control panel at
  `http://localhost:5058/`).
- For print-layout problems, attach the **print format HTML** (redact any
  customer data) and a photo/scan of the wrong output if possible.

### Suggesting enhancements

- Open a [Feature Request](.github/ISSUE_TEMPLATE/feature_request.yml).
- Explain the POS / fiscal context — receipts are country-specific, so the
  "why" matters as much as the "what".
- Literal-mode receipt layout questions belong in
  [Discussions](../../discussions) rather than issues.

### Pull requests

1. Fork the repo and create a branch from `main`
   (`feat/…`, `fix/…`, `docs/…`).
2. Keep changes focused — one topic per PR.
3. If you change parsing or byte generation, add or update tests.
4. Run the test suite locally (see below) and make sure it passes.
5. Update the README / `docs/` landing page when behaviour or the public
   API changes.
6. Commit messages follow
   [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `ci:`, `test:`, `refactor:`) — this is what
   the project already uses.

## Local development

```bash
cd api-printer-service
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python main.py            # serves on http://127.0.0.1:5058
```

Run the tests:

```bash
pytest tests/
```

The suite covers modeled-mode extraction, `data-print` pass-through blocks,
full literal mode, ESC/POS byte output, the sync/threadpool endpoint
contract, and Windows printer enumeration.

## Landing page / GitHub Pages

The product landing page lives in [`docs/index.html`](docs/index.html) and is
deployed automatically to GitHub Pages by
[`.github/workflows/pages.yml`](.github/workflows/pages.yml) on every push to
`main` that touches `docs/**`. It is a single self-contained HTML file —
no build step, no external assets — so edits are plain HTML/CSS.

## CI expectations

- **ci.yml** runs the pytest suite across supported platforms/Python versions.
- **release.yml** builds the self-contained installers (Linux `.run`,
  Windows `.exe`) and attaches them to GitHub Releases.
- **pages.yml** deploys the landing page to GitHub Pages.

A PR is ready for review when CI is green and the diff is complete (no
stray debug code, no unrelated files).

## Licensing

By contributing you agree that your contributions will be licensed under
the [MIT License](LICENSE) that covers the project.
