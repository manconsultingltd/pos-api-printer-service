"""
Endpoints that hit the spooler, the parser or the settings file must be
plain sync `def`s: FastAPI then runs them in a worker thread, keeping the
event loop — and with it /health and /api/printers — responsive while a
print job blocks. An `async def` here would run the blocking call on the
event loop itself and freeze the whole service.
"""

import inspect

from fastapi.routing import APIRoute

import main

BLOCKING_PATHS = {
    "/api/printers",
    "/api/status/{printer_name}",
    "/api/print",
    "/api/print-html",
    "/api/cash-drawer",
    "/api/jobs",
    "/api/jobs/{job_id}",
    "/api/test-print",
    "/api/settings",
}


def test_blocking_endpoints_run_in_threadpool():
    seen = set()
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.path in BLOCKING_PATHS:
            seen.add(route.path)
            assert not inspect.iscoroutinefunction(route.endpoint), (
                f"{route.path} must be a sync def (threadpool), not async"
            )
    assert seen == BLOCKING_PATHS
