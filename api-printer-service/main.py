"""
API Printer Service - FastAPI Application
Provides REST API for receipt printing via CUPS (thermal, dot matrix, laser, etc.)

Port: 5058
Base URL: http://localhost:5058
"""

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from escpos_generator import ESCPOSGenerator
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from html_parser import HtmlReceiptParser
from printer_manager import PrinterManager
from pydantic import BaseModel, Field

import service_controller

# Configure logging - both file and console
# Path is platform-aware (or overridden via LOG_FILE env var)
import os as _os
log_file = Config.LOG_FILE
_os.makedirs(_os.path.dirname(_os.path.abspath(log_file)), exist_ok=True)
log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# File handler
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.DEBUG)

# Console handler (for systemd journal)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

# Configure root logger
logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)

# Log startup
logger.info("=" * 60)
logger.info("API Printer Service starting up")
logger.info(f"Log file: {log_file}")
logger.info(f"Port: {Config.PORT}")
logger.info("=" * 60)

# Initialize FastAPI app
app = FastAPI(
    title="API Printer Service",
    description="REST API for receipt printing (thermal, dot matrix, laser, etc.)",
    version="1.0.0",
)

# Configure CORS for local access
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.CORS_ORIGINS,
    allow_credentials=False,  # Wildcard origins do not support credentials
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add Private Network Access support for modern browser requests from secure origins to localhost
from fastapi import Request
@app.middleware("http")
async def allow_private_network_middleware(request: Request, call_next):
    response = await call_next(request)
    if "access-control-request-private-network" in request.headers:
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

# Initialize printer manager
printer_manager = PrinterManager()


# === Startup Event - Ensure USB Printer is Configured ===


@app.on_event("startup")
async def startup_event():
    """
    Ensure USB printer is properly configured on service start.
    USB auto-setup only runs on Linux, and only if the optional
    /usr/local/bin/setup-usb-printer.sh helper is present.
    """
    if Config.IS_LINUX:
        logger.info("Running USB printer setup check...")
        try:
            result = subprocess.run(
                ["/usr/local/bin/setup-usb-printer.sh"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.stdout:
                logger.info(f"Printer setup output: {result.stdout.strip()}")
            if result.stderr:
                logger.warning(f"Printer setup warnings: {result.stderr.strip()}")
            if result.returncode == 0:
                logger.info("USB printer setup completed successfully")
            else:
                logger.warning(f"Printer setup exited with code {result.returncode}")
        except subprocess.TimeoutExpired:
            logger.error("Printer setup script timed out after 30 seconds")
        except FileNotFoundError:
            logger.debug("USB printer setup script not found — skipping")
        except Exception as e:
            logger.error(f"Error running printer setup: {e}")

    # Log current printer status
    printers = printer_manager.list_printers()
    if printers:
        logger.info(f"Available printers: {[p['name'] for p in printers]}")
    else:
        logger.warning("No printers found in CUPS")


# === Request/Response Models ===


class InvoiceItem(BaseModel):
    """Invoice line item"""

    name: str
    qty: float
    rate: float
    amount: float


class InvoiceData(BaseModel):
    """Invoice data for receipt generation"""

    company: Optional[str] = "POS"
    invoice_number: Optional[str] = None
    date: Optional[str] = None
    cashier: Optional[str] = None
    customer: Optional[str] = None
    items: List[InvoiceItem] = []
    subtotal: float = 0.0
    tax: float = 0.0
    discount: float = 0.0
    total: float = 0.0
    payment_method: Optional[str] = "Cash"
    amount_paid: float = 0.0
    change: float = 0.0
    footer_text: Optional[str] = "Thank you!"


class PrintOptions(BaseModel):
    """Print job options"""

    open_drawer: bool = False
    paper_width: int = Field(default=58, description="Paper width in mm (58 or 80)")


class PrintRequest(BaseModel):
    """Print request payload"""

    printer: str = Field(default_factory=Config.get_default_printer, description="Printer name")
    type: str = Field(default="ESC_POS", description="Printer type (ESC_POS, etc.)")
    size: int = Field(default=58, description="Paper size in mm")
    data: InvoiceData
    options: Optional[PrintOptions] = None


class PrintResponse(BaseModel):
    """Print response"""

    success: bool
    job_id: Optional[int] = None
    drawer_opened: bool = False
    message: str


class CashDrawerRequest(BaseModel):
    """Cash drawer request"""

    printer: str = Field(default_factory=Config.get_default_printer, description="Printer name")


class TestPrintRequest(BaseModel):
    """Test print request"""

    printer: str = Field(default_factory=Config.get_default_printer, description="Printer name")
    paper_width: int = Field(default=58, description="Paper width in mm")


class PrintHtmlRequest(BaseModel):
    """Print from HTML request - parses HTML directly, no JSON needed"""

    printer: str = Field(default_factory=Config.get_default_printer, description="Printer name")
    html: str = Field(..., description="Raw HTML content of the receipt")
    paper_width: int = Field(default=58, description="Paper width in mm (58 or 80)")
    open_drawer: bool = Field(
        default=False, description="Open cash drawer after printing"
    )


# === API Endpoints ===


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "api-printer-service",
        "timestamp": datetime.now().isoformat(),
    }


# NOTE: every endpoint below that touches the spooler (win32print / lpstat /
# lpr), the parser, or the settings file is a plain `def`, NOT `async def`.
# FastAPI runs sync endpoints in a worker thread; as `async def` their
# blocking calls would run on the event loop itself, and one slow print or
# a hung spooler call would freeze /health and /api/printers for everyone.
@app.get("/api/printers")
def list_printers():
    """
    List all available CUPS printers

    Returns:
        List of printers with status
    """
    try:
        printers = printer_manager.list_printers()
        return {"success": True, "printers": printers, "count": len(printers)}
    except Exception as e:
        logger.error(f"Error listing printers: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list printers: {str(e)}",
        )


@app.get("/api/status/{printer_name}")
def get_printer_status(printer_name: str):
    """
    Get status of a specific printer

    Args:
        printer_name: Name of the printer

    Returns:
        Printer status details
    """
    try:
        printer_status = printer_manager.get_printer_status(printer_name)

        if printer_status is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Printer '{printer_name}' not found",
            )

        return {"success": True, "printer": printer_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting printer status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get printer status: {str(e)}",
        )


@app.post("/api/print", response_model=PrintResponse)
def print_receipt(request: PrintRequest):
    """
    Print a receipt to thermal printer

    Args:
        request: Print request with invoice data

    Returns:
        Print response with job ID
    """
    try:
        logger.info(
            f"Print request received: invoice={request.data.invoice_number}, items={len(request.data.items)}"
        )
        logger.debug(f"Full request data: {request.data.dict()}")

        # Validate printer exists
        if not printer_manager.test_printer(request.printer):
            logger.error(f"Printer not found or disabled: {request.printer}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Printer '{request.printer}' not found or not enabled",
            )

        # Determine paper width
        paper_width = request.size
        if request.options and request.options.paper_width:
            paper_width = request.options.paper_width

        logger.debug(f"Using paper width: {paper_width}mm")

        # Generate ESC/POS commands
        logger.debug("Generating ESC/POS commands...")
        generator = ESCPOSGenerator(paper_width=paper_width)
        invoice_dict = request.data.dict()
        escpos_bytes = generator.generate_receipt(invoice_dict)
        logger.debug(f"Generated {len(escpos_bytes)} bytes of ESC/POS data")

        # Print to CUPS
        logger.debug(f"Sending to CUPS printer: {request.printer}")
        job_id = printer_manager.print_raw_bytes(
            printer_name=request.printer,
            data=escpos_bytes,
            job_title=f"Receipt {request.data.invoice_number or 'N/A'}",
        )

        if job_id is None:
            logger.error("CUPS failed to accept print job")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to submit print job",
            )

        # Open cash drawer if requested
        drawer_opened = False
        if request.options and request.options.open_drawer:
            logger.debug("Opening cash drawer...")
            drawer_cmd = ESCPOSGenerator.cash_drawer_pulse()
            drawer_result = printer_manager.print_raw_bytes(
                printer_name=request.printer,
                data=drawer_cmd,
                job_title="Cash Drawer Open",
            )
            drawer_opened = drawer_result is not None
            logger.debug(f"Drawer open result: {drawer_opened}")

        logger.info(
            f"✓ Receipt printed successfully: job_id={job_id}, drawer={drawer_opened}"
        )

        return PrintResponse(
            success=True,
            job_id=job_id,
            drawer_opened=drawer_opened,
            message=f"Receipt printed successfully (Job ID: {job_id})",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error printing receipt: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Print failed: {str(e)}",
        )


@app.post("/api/print-html", response_model=PrintResponse)
def print_from_html(request: PrintHtmlRequest):
    """
    Print a receipt from raw HTML - no JSON needed!

    Parses the HTML to extract receipt data (company, items, totals, etc.)
    and generates ESC/POS commands for thermal printing.

    Args:
        request: Print request with raw HTML content

    Returns:
        Print response with job ID
    """
    try:
        logger.info(
            f"HTML print request received, HTML length: {len(request.html)} chars"
        )

        # Validate printer exists
        if not printer_manager.test_printer(request.printer):
            logger.error(f"Printer not found or disabled: {request.printer}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Printer '{request.printer}' not found or not enabled",
            )

        # Parse HTML to extract invoice data
        logger.debug("Parsing HTML to extract receipt data...")
        parser = HtmlReceiptParser()
        invoice_data = parser.parse(request.html)

        logger.info(
            f"Parsed receipt: invoice={invoice_data.get('invoice_number')}, "
            f"items={len(invoice_data.get('items', []))}, "
            f"total={invoice_data.get('total')}"
        )
        logger.debug(f"Full parsed data: {invoice_data}")

        # Generate ESC/POS commands
        logger.debug(
            f"Generating ESC/POS commands for {request.paper_width}mm paper..."
        )
        generator = ESCPOSGenerator(paper_width=request.paper_width)
        escpos_bytes = generator.generate_receipt(invoice_data)
        logger.debug(f"Generated {len(escpos_bytes)} bytes of ESC/POS data")

        # Prepend cash drawer command so it opens immediately
        drawer_cmd = ESCPOSGenerator.cash_drawer_pulse()
        combined_data = drawer_cmd + escpos_bytes
        logger.debug("Cash drawer command prepended to receipt data")

        # Print to CUPS (single job: drawer + receipt)
        logger.debug(f"Sending to CUPS printer: {request.printer}")
        job_id = printer_manager.print_raw_bytes(
            printer_name=request.printer,
            data=combined_data,
            job_title=f"Receipt {invoice_data.get('invoice_number') or 'N/A'}",
        )

        if job_id is None:
            logger.error("CUPS failed to accept print job")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to submit print job",
            )

        drawer_opened = True

        logger.info(
            f"✓ HTML receipt printed successfully: job_id={job_id}, drawer={drawer_opened}"
        )

        return PrintResponse(
            success=True,
            job_id=job_id,
            drawer_opened=drawer_opened,
            message=f"Receipt printed from HTML (Job ID: {job_id})",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error printing HTML receipt: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"HTML print failed: {str(e)}",
        )


@app.post("/api/cash-drawer")
def open_cash_drawer(request: CashDrawerRequest):
    """
    Open cash drawer

    Args:
        request: Cash drawer request with printer name

    Returns:
        Success status
    """
    try:
        # Validate printer exists
        if not printer_manager.test_printer(request.printer):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Printer '{request.printer}' not found or not enabled",
            )

        # Generate drawer pulse command
        drawer_cmd = ESCPOSGenerator.cash_drawer_pulse()

        # Send to printer
        job_id = printer_manager.print_raw_bytes(
            printer_name=request.printer, data=drawer_cmd, job_title="Cash Drawer Open"
        )

        if job_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to open cash drawer",
            )

        logger.info(f"Cash drawer opened: job_id={job_id}")

        return {"success": True, "job_id": job_id, "message": "Cash drawer opened"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error opening cash drawer: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to open cash drawer: {str(e)}",
        )


@app.get("/api/jobs")
def list_print_jobs(printer: Optional[str] = None):
    """
    List print jobs

    Args:
        printer: Optional printer name to filter jobs

    Returns:
        List of print jobs
    """
    try:
        jobs = printer_manager.list_jobs(printer_name=printer)
        return {"success": True, "jobs": jobs, "count": len(jobs)}
    except Exception as e:
        logger.error(f"Error listing jobs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list jobs: {str(e)}",
        )


@app.delete("/api/jobs/{job_id}")
def cancel_print_job(job_id: int):
    """
    Cancel a print job

    Args:
        job_id: Job ID to cancel

    Returns:
        Success status
    """
    try:
        success = printer_manager.cancel_job(job_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found or already completed",
            )

        return {"success": True, "message": f"Job {job_id} cancelled"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling job: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel job: {str(e)}",
        )


@app.post("/api/test-print")
def test_print(request: TestPrintRequest):
    """
    Print a test receipt

    Args:
        request: Test print request

    Returns:
        Print response
    """
    try:
        # Validate printer exists
        if not printer_manager.test_printer(request.printer):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Printer '{request.printer}' not found or not enabled",
            )

        # Generate test receipt
        generator = ESCPOSGenerator(paper_width=request.paper_width)
        test_bytes = generator.generate_test_receipt()

        # Print to CUPS
        job_id = printer_manager.print_raw_bytes(
            printer_name=request.printer, data=test_bytes, job_title="Test Receipt"
        )

        if job_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to submit test print job",
            )

        logger.info(f"Test receipt printed: job_id={job_id}")

        return {
            "success": True,
            "job_id": job_id,
            "message": f"Test receipt printed (Job ID: {job_id})",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error printing test receipt: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Test print failed: {str(e)}",
        )


class ClientLogRequest(BaseModel):
    """Client log entry from POSAwesome"""

    level: str
    message: str
    data: Optional[Dict[str, Any]] = None
    timestamp: str


@app.post("/api/log")
async def log_from_client(request: ClientLogRequest):
    """
    Log messages from POSAwesome browser client
    Allows client-side errors to be logged to server file for SSH access

    Args:
        request: Log entry from client

    Returns:
        Success status
    """
    try:
        # Write to same log file with [CLIENT] prefix
        client_logger = logging.getLogger("client")
        log_level = getattr(logging, request.level.upper(), logging.INFO)

        log_message = f"[CLIENT] {request.message}"
        if request.data:
            log_message += f" | Data: {request.data}"

        client_logger.log(log_level, log_message)

        return {"success": True}
    except Exception as e:
        # Don't fail client operations if logging fails
        logger.warning(f"Failed to log client message: {e}")
        return {"success": False, "error": str(e)}


# === Settings ===


class SettingsModel(BaseModel):
    """Persisted service settings"""

    default_printer: Optional[str] = None


@app.get("/api/settings")
def get_settings():
    """Return persisted service settings (default printer, etc.)."""
    return {"default_printer": Config.get_default_printer()}


@app.put("/api/settings")
def update_settings(settings: SettingsModel):
    """Update persisted service settings. Currently only default_printer."""
    try:
        if settings.default_printer is not None:
            Config.set_default_printer(settings.default_printer)
            logger.info(f"Default printer set to: {settings.default_printer}")
        return {"success": True, "default_printer": Config.get_default_printer()}
    except OSError as e:
        logger.error(f"Failed to persist settings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist settings: {e}",
        )


# === Control Panel (served at /) ===


def _bundle_dir() -> Path:
    """
    Return the directory that holds bundled data files.

    When running under PyInstaller (Linux --onefile), `sys._MEIPASS` points
    at the extracted bundle root. Otherwise fall back to the source tree.
    """
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


_HTML_CACHE: Optional[str] = None


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def control_panel() -> HTMLResponse:
    """Serve the static HTML control panel."""
    global _HTML_CACHE
    if _HTML_CACHE is None:
        try:
            _HTML_CACHE = (_bundle_dir() / "web" / "index.html").read_text(
                encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"Control panel asset missing: {e}")
            return HTMLResponse(
                "<h1>Control panel unavailable</h1>"
                "<p>Installer did not ship the UI assets.</p>",
                status_code=500,
            )
    return HTMLResponse(_HTML_CACHE)


# === Service control endpoints (stop / restart from within) ===


class ServiceStatus(BaseModel):
    state: str
    platform: str


@app.get("/api/service/status", response_model=ServiceStatus)
async def service_status() -> ServiceStatus:
    """
    Report the service as 'active' from its own perspective.

    Callers infer 'unreachable' from a connection error; deep platform-level
    state introspection is not needed here.
    """
    return ServiceStatus(state="active", platform=service_controller.platform_name())


@app.post("/api/service/restart", status_code=status.HTTP_202_ACCEPTED)
async def service_restart() -> Dict[str, Any]:
    """Schedule a detached restart and return before the process exits."""
    try:
        service_controller.restart_async()
        logger.info("Restart requested via /api/service/restart")
        return {"success": True, "message": "Restart scheduled"}
    except Exception as e:
        logger.error(f"Restart scheduling failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to schedule restart: {e}",
        )


@app.post("/api/service/stop", status_code=status.HTTP_202_ACCEPTED)
async def service_stop() -> Dict[str, Any]:
    """Schedule a detached stop and return before the process exits."""
    try:
        service_controller.stop_async()
        logger.info("Stop requested via /api/service/stop")
        return {"success": True, "message": "Stop scheduled"}
    except Exception as e:
        logger.error(f"Stop scheduling failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to schedule stop: {e}",
        )


# === Application Entry Point ===

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting API Printer Service on {Config.HOST}:{Config.PORT}")

    uvicorn.run(
        app, host=Config.HOST, port=Config.PORT, log_level=Config.LOG_LEVEL.lower()
    )
