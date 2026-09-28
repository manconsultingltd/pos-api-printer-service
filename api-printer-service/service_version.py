"""
Service version — the single version stamp the updater CLI reads.

`0.0.0-dev` is a placeholder: CI (release.yml) seds the real
`1.1.<run_number>` into this file AND main.py before building, so the
shipped tree always reports the version it was built with.
"""

VERSION = "0.0.0-dev"
