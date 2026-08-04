"""
Cross-platform Printer Manager
- Linux / macOS: communicates with CUPS via lpr, lpstat, cancel
- Windows: communicates with the Windows print spooler via win32print
"""

import logging
import subprocess
import sys
from typing import Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)

_IS_WINDOWS = Config.IS_WINDOWS


class PrinterManager:
    """Manages printer operations (CUPS on Linux/macOS, win32print on Windows)"""

    def __init__(self):
        self.lpr_path = Config.LPR_PATH
        self.lpstat_path = Config.LPSTAT_PATH
        self.cancel_path = Config.CANCEL_PATH

    def list_printers(self) -> List[Dict[str, str]]:
        """
        List all available printers.

        Returns:
            List of printer dictionaries with name and status
        """
        if _IS_WINDOWS:
            return self._list_printers_windows()
        return self._list_printers_cups()

    def _list_printers_windows(self) -> List[Dict[str, str]]:
        try:
            import win32print
            # GetDefaultPrinter raises when the profile has no default
            # printer — common for the SYSTEM account the service runs
            # under. That must not wipe out the whole enumeration.
            try:
                default_printer = win32print.GetDefaultPrinter()
            except Exception:
                default_printer = None
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            raw = win32print.EnumPrinters(flags, None, 2)
            printers = []
            for info in raw:
                name = info["pPrinterName"]
                status_code = info.get("Status", 0)
                status = "idle" if status_code == 0 else f"status_{status_code}"
                printers.append({
                    "name": name,
                    "status": status,
                    "is_default": name == default_printer,
                })
            return printers
        except Exception as e:
            logger.error(f"Error listing Windows printers: {e}")
            return []

    def _list_printers_cups(self) -> List[Dict[str, str]]:
        try:
            result = subprocess.run(
                [self.lpstat_path, "-p", "-d"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.error(f"lpstat failed: {result.stderr}")
                return []

            lines = result.stdout.splitlines()

            default_printer = None
            for line in lines:
                line = line.strip()
                if line.startswith("system default destination:"):
                    default_printer = line.split(":")[-1].strip()
                    break

            printers = []
            for line in lines:
                line = line.strip()
                if line.startswith("printer "):
                    parts = line.split()
                    if len(parts) >= 4:
                        printer_name = parts[1]
                        status = parts[3].rstrip(".")
                        printers.append({
                            "name": printer_name,
                            "status": status,
                            "is_default": printer_name == default_printer,
                        })
            return printers

        except subprocess.TimeoutExpired:
            logger.error("lpstat command timed out")
            return []
        except Exception as e:
            logger.error(f"Error listing printers: {e}")
            return []

    def get_printer_status(self, printer_name: str) -> Optional[Dict[str, any]]:
        """
        Get detailed status of a specific printer.

        Returns:
            Dictionary with printer status details or None if not found
        """
        if _IS_WINDOWS:
            return self._get_printer_status_windows(printer_name)
        return self._get_printer_status_cups(printer_name)

    def _get_printer_status_windows(self, printer_name: str) -> Optional[Dict[str, any]]:
        try:
            import win32print
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            all_printers = {p["pPrinterName"]: p for p in win32print.EnumPrinters(flags, None, 2)}
            if printer_name not in all_printers:
                logger.warning(f"Printer '{printer_name}' not found")
                return None
            info = all_printers[printer_name]
            status_code = info.get("Status", 0)
            return {
                "name": printer_name,
                "exists": True,
                "state": "idle" if status_code == 0 else f"status_{status_code}",
                "enabled": True,
                "disabled": False,
                "raw_status": f"Windows printer status code: {status_code}",
            }
        except Exception as e:
            logger.error(f"Error getting Windows printer status: {e}")
            return None

    def _get_printer_status_cups(self, printer_name: str) -> Optional[Dict[str, any]]:
        try:
            result = subprocess.run(
                [self.lpstat_path, "-p", printer_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning(f"Printer '{printer_name}' not found")
                return None

            output = result.stdout.strip()
            status: Dict[str, any] = {"name": printer_name, "exists": True, "raw_status": output}

            if "idle" in output.lower():
                status["state"] = "idle"
            elif "processing" in output.lower() or "printing" in output.lower():
                status["state"] = "processing"
            elif "stopped" in output.lower():
                status["state"] = "stopped"
            else:
                status["state"] = "unknown"

            status["enabled"] = "enabled" in output.lower()
            status["disabled"] = "disabled" in output.lower()
            return status

        except subprocess.TimeoutExpired:
            logger.error(f"lpstat timed out for printer '{printer_name}'")
            return None
        except Exception as e:
            logger.error(f"Error getting printer status: {e}")
            return None

    def print_raw_bytes(
        self, printer_name: str, data: bytes, job_title: str = "Thermal Receipt"
    ) -> Optional[int]:
        """
        Print raw bytes to printer.

        Returns:
            Job ID (integer) or None if failed
        """
        if _IS_WINDOWS:
            return self._print_raw_bytes_windows(printer_name, data, job_title)
        return self._print_raw_bytes_cups(printer_name, data, job_title)

    def _print_raw_bytes_windows(
        self, printer_name: str, data: bytes, job_title: str
    ) -> Optional[int]:
        """Send raw ESC/POS bytes to a Windows printer via win32print RAW data type."""
        try:
            import win32print
            hPrinter = win32print.OpenPrinter(printer_name)
            try:
                job_id = win32print.StartDocPrinter(hPrinter, 1, (job_title, None, "RAW"))
                try:
                    win32print.StartPagePrinter(hPrinter)
                    win32print.WritePrinter(hPrinter, data)
                    win32print.EndPagePrinter(hPrinter)
                finally:
                    win32print.EndDocPrinter(hPrinter)
            finally:
                win32print.ClosePrinter(hPrinter)
            logger.info(f"Print job submitted (Windows): job_id={job_id}")
            return job_id
        except Exception as e:
            logger.error(f"Windows print error: {e}")
            return None

    def _print_raw_bytes_cups(
        self, printer_name: str, data: bytes, job_title: str
    ) -> Optional[int]:
        """Send raw bytes to a CUPS printer via lpr."""
        try:
            result = subprocess.run(
                [self.lpr_path, "-P", printer_name, "-T", job_title, "-o", "raw"],
                input=data,
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.error(f"lpr failed: {result.stderr.decode('utf-8', errors='ignore')}")
                return None

            job_result = subprocess.run(
                [self.lpstat_path, "-W", "not-completed"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if job_result.returncode == 0:
                lines = job_result.stdout.strip().splitlines()
                if lines:
                    last_job = lines[-1].split()[0]
                    if "-" in last_job:
                        job_id = int(last_job.split("-")[-1])
                        logger.info(f"Print job submitted: {job_id}")
                        return job_id

            logger.info("Print job submitted (no job ID)")
            return 0

        except subprocess.TimeoutExpired:
            logger.error("lpr command timed out")
            return None
        except Exception as e:
            logger.error(f"Error printing: {e}")
            return None

    def list_jobs(self, printer_name: Optional[str] = None) -> List[Dict[str, any]]:
        """List pending/active print jobs."""
        if _IS_WINDOWS:
            return self._list_jobs_windows(printer_name)
        return self._list_jobs_cups(printer_name)

    def _list_jobs_windows(self, printer_name: Optional[str] = None) -> List[Dict[str, any]]:
        try:
            import win32print
            target = printer_name or Config.DEFAULT_PRINTER
            try:
                hPrinter = win32print.OpenPrinter(target)
            except Exception:
                return []
            try:
                raw_jobs = win32print.EnumJobs(hPrinter, 0, -1, 1)
            finally:
                win32print.ClosePrinter(hPrinter)
            jobs = []
            for job in raw_jobs:
                jobs.append({
                    "job_id": job["JobId"],
                    "printer": target,
                    "user": job.get("pUserName", ""),
                    "size": job.get("Size", 0),
                    "raw": str(job),
                })
            return jobs
        except Exception as e:
            logger.error(f"Error listing Windows jobs: {e}")
            return []

    def _list_jobs_cups(self, printer_name: Optional[str] = None) -> List[Dict[str, any]]:
        try:
            cmd = [self.lpstat_path, "-W", "not-completed"]
            if printer_name:
                cmd.extend(["-p", printer_name])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return []
            jobs = []
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5:
                    job_printer = parts[0]
                    if "-" in job_printer:
                        printer, job_id = job_printer.rsplit("-", 1)
                        jobs.append({
                            "job_id": int(job_id),
                            "printer": printer,
                            "user": parts[1],
                            "size": parts[2],
                            "raw": line,
                        })
            return jobs
        except subprocess.TimeoutExpired:
            logger.error("lpstat timed out")
            return []
        except Exception as e:
            logger.error(f"Error listing jobs: {e}")
            return []

    def cancel_job(self, job_id: int) -> bool:
        """Cancel a print job. Returns True if successful."""
        if _IS_WINDOWS:
            return self._cancel_job_windows(job_id)
        return self._cancel_job_cups(job_id)

    def _cancel_job_windows(self, job_id: int) -> bool:
        try:
            import win32print
            # Find the printer that owns this job
            for info in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS, None, 2
            ):
                name = info["pPrinterName"]
                try:
                    hPrinter = win32print.OpenPrinter(name)
                    try:
                        jobs = win32print.EnumJobs(hPrinter, 0, -1, 1)
                        for job in jobs:
                            if job["JobId"] == job_id:
                                win32print.SetJob(hPrinter, job_id, 0, None, win32print.JOB_CONTROL_DELETE)
                                logger.info(f"Job {job_id} cancelled (Windows)")
                                return True
                    finally:
                        win32print.ClosePrinter(hPrinter)
                except Exception:
                    continue
            logger.warning(f"Job {job_id} not found on any Windows printer")
            return False
        except Exception as e:
            logger.error(f"Error cancelling Windows job: {e}")
            return False

    def _cancel_job_cups(self, job_id: int) -> bool:
        try:
            result = subprocess.run(
                [self.cancel_path, str(job_id)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info(f"Job {job_id} cancelled")
                return True
            else:
                logger.error(f"Failed to cancel job {job_id}: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("cancel command timed out")
            return False
        except Exception as e:
            logger.error(f"Error cancelling job: {e}")
            return False

    def test_printer(self, printer_name: str) -> bool:
        """
        Test if printer is accessible

        Args:
            printer_name: Name of the printer to test

        Returns:
            True if printer exists and is enabled
        """
        status = self.get_printer_status(printer_name)
        return status is not None and status.get("enabled", False)
