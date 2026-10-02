# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Configurable print copies (1, 2 or 3; default 1): **Copies** dropdown in the
  web control panel and the Windows/Linux desktop apps, `copies` in `/api/settings`, and an optional `copies` field on
  `/api/print` and `/api/print-html`. Each copy is cut separately.
- Product landing page (`docs/index.html`) deployed to GitHub Pages via
  `.github/workflows/pages.yml`.
- Open-source community files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue templates, and PR template.
