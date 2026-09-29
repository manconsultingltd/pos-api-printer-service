# Security Policy

## Supported versions

The service ships with a standalone self-update worker; users are expected to
stay on the **latest release**. Security fixes are made against `main` and
released as a new version rather than backported.

| Version | Supported |
| ------- | --------- |
| latest release | ✅ |
| older releases | ❌ (please update via the built-in updater) |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead:

1. Use GitHub's private vulnerability reporting on this repository
   (*Security* tab → *Report a vulnerability*), or
2. Contact the maintainer **MAN Consulting Ltd** via the contact details on
   the [GitHub organization profile](https://github.com/manconsultingltd).

Include as much of the following as you can:

- Type of issue (e.g. buffer overflow, injection, privilege escalation)
- Full paths of source file(s) related to the manifestation of the issue
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit it

## What is in scope

- The HTTP service itself (`api-printer-service/main.py` and endpoints)
- The HTML parsing pipeline (`html_parser.py`, `escpos_generator.py`)
- The installers and self-update mechanism (supply-chain integrity)
- The control panel served at `http://localhost:5058/`

## What is out of scope / by design

- **Network exposure**: the service intentionally binds to `127.0.0.1`
  (localhost only). Reports that require the operator to deliberately expose
  the port to other interfaces are out of scope.
- **Browser fallback**: POSAwesome falling back to the browser print dialog
  when the service is unreachable is a documented feature, not a vulnerability.
- **Receipt content**: layout issues from user-authored print formats are
  functional questions, not security ones.

## Response targets

- Acknowledgement: within **7 days**
- Status update / fix estimate: within **30 days**
- Coordinated disclosure: we credit reporters in the release notes unless they
  prefer to remain anonymous.
