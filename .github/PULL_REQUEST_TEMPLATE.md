# Pull Request

## Summary

<!-- One or two sentences: what does this PR do? -->

## Type of change

- [ ] 🐞 Bug fix (`fix:`)
- [ ] ✨ New feature (`feat:`)
- [ ] 📝 Docs / landing page (`docs:`)
- [ ] 🔧 CI / build (`ci:`)
- [ ] ♻️ Refactor (`refactor:`)
- [ ] 🧪 Tests (`test:`)

## Affected area(s)

- [ ] `main.py` (HTTP API)
- [ ] `html_parser.py` (literal / modeled parsing)
- [ ] `escpos_generator.py` (ESC/POS bytes)
- [ ] `printer_manager.py` (CUPS / Windows spooler)
- [ ] Installers / updater
- [ ] Control panel (`web/`)
- [ ] Landing page (`docs/`)
- [ ] CI / workflows

## Checklist

- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] `pytest tests/` passes locally
- [ ] Tests added/updated for parsing or byte-generation changes
- [ ] README / `docs/` updated if behaviour or public API changed
- [ ] `CHANGELOG.md` updated under **[Unreleased]** (for user-facing changes)
- [ ] No customer data in examples, logs or fixtures
