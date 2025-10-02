"""Helpers for driving a browser to fetch bibliography PDFs automatically."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys
from typing import List, Optional, Sequence

DEFAULT_SELECTORS: tuple[str, ...] = (
    "a[download]",
    "a[href$='.pdf']",
    "a:has-text(\"PDF\")",
    "a:has-text(\"Download PDF\")",
    "button:has-text(\"PDF\")",
    "button:has-text(\"Download\")",
    "a:has-text(\"Full Text\")",
)


@dataclass
class AutomationResult:
    """Outcome of a single automation attempt."""

    attempted: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)

    def record(self, message: str) -> None:
        self.logs.append(message)


@dataclass
class SetupReport:
    """Information about preparing the Playwright environment."""

    succeeded: bool = False
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)

    def record(self, message: str) -> None:
        self.logs.append(message)


_PLAYWRIGHT_READY = False
_BROWSERS_READY: set[str] = set()


def _run_subprocess(command: Sequence[str], report: SetupReport) -> bool:
    """Execute *command* and capture stdout/stderr into ``report``."""

    quoted = " ".join(command)
    report.record(f"Executing: {quoted}")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.stdout:
        report.record(completed.stdout.strip())
    if completed.stderr:
        report.record(completed.stderr.strip())
    if completed.returncode != 0:
        report.error = (
            report.error
            or f"Command '{quoted}' exited with status {completed.returncode}."
        )
        return False
    return True


def _ensure_playwright_setup(browser: str, report: SetupReport, *, install_browser: bool) -> bool:
    """Ensure Playwright and the requested browser runtime are available."""

    global _PLAYWRIGHT_READY
    if not _PLAYWRIGHT_READY:
        try:
            import importlib

            importlib.import_module("playwright.async_api")
            _PLAYWRIGHT_READY = True
            report.record("Playwright already installed.")
        except ImportError:
            report.record("Playwright not found; attempting to install via pip.")
            command = [sys.executable, "-m", "pip", "install", "playwright"]
            if not _run_subprocess(command, report):
                report.error = report.error or (
                    "Failed to install Playwright automatically. "
                    "Install it manually with 'pip install playwright'."
                )
                return False
            try:
                import importlib

                importlib.import_module("playwright.async_api")
                _PLAYWRIGHT_READY = True
                report.record("Playwright installed successfully.")
            except ImportError as exc:  # pragma: no cover - defensive guard
                report.error = (
                    "Playwright remains unavailable after installation attempt: "
                    f"{exc}"
                )
                return False

    if install_browser and browser not in _BROWSERS_READY:
        report.record(f"Ensuring Playwright browser '{browser}' is installed.")
        command = [sys.executable, "-m", "playwright", "install", browser]
        if not _run_subprocess(command, report):
            report.error = report.error or (
                f"Failed to install the Playwright '{browser}' browser runtime."
            )
            return False
        _BROWSERS_READY.add(browser)
        report.record(f"Browser '{browser}' installation confirmed.")

    return True


async def _run_in_browser(
    candidate_url: str,
    download_path: Path,
    *,
    browser: str,
    selectors: Sequence[str],
    headless: bool,
    login_wait_seconds: float,
    click_timeout_seconds: float,
    result: AutomationResult,
) -> Optional[Path]:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser_type = getattr(playwright, browser)
        except AttributeError as exc:  # pragma: no cover - defensive guard
            raise ValueError(f"Unsupported browser '{browser}'") from exc

        launched_browser = await browser_type.launch(headless=headless)
        try:
            context = await launched_browser.new_context(accept_downloads=True)
            try:
                page = await context.new_page()
                await page.goto(candidate_url, wait_until="load")
                if login_wait_seconds:
                    await page.wait_for_timeout(int(login_wait_seconds * 1000))

                errors: List[str] = []
                for selector in selectors:
                    locator = page.locator(selector)
                    try:
                        if await locator.count() == 0:
                            continue

                        async with page.expect_download(timeout=int(click_timeout_seconds * 1000)) as download_info:
                            await locator.first.click()
                        download = await download_info.value
                        suggested = download.suggested_filename or "bibliography.pdf"
                        target = download_path / suggested
                        await download.save_as(str(target))
                        result.record(f"Downloaded via selector '{selector}' -> {target}")
                        return target
                    except PlaywrightTimeoutError:
                        errors.append(f"No download triggered after clicking '{selector}'.")
                    except Exception as exc:  # pragma: no cover - best effort logging
                        errors.append(f"Error clicking '{selector}': {exc}")

                if errors:
                    result.error = "\n".join(errors)
                return None
            finally:
                await context.close()
        finally:
            await launched_browser.close()


def attempt_automated_pdf_download(
    candidate_url: str,
    *,
    download_dir: Path | None = None,
    browser: str = "chromium",
    selectors: Sequence[str] | None = None,
    headless: bool = True,
    login_wait_seconds: float = 12.0,
    click_timeout_seconds: float = 8.0,
) -> AutomationResult:
    """Attempt to fetch a bibliography PDF using Playwright."""

    if not candidate_url:
        return AutomationResult(
            attempted=False, error="No URL provided for automated download."
        )

    download_path = Path(download_dir or Path.cwd() / "downloads")
    download_path.mkdir(parents=True, exist_ok=True)

    selectors_to_try = list(dict.fromkeys((selectors or []) + list(DEFAULT_SELECTORS)))
    result = AutomationResult(attempted=False)

    setup_report = SetupReport(succeeded=False)
    if not _ensure_playwright_setup(browser, setup_report, install_browser=True):
        result.error = setup_report.error
        result.logs.extend(setup_report.logs)
        return result

    result.logs.extend(setup_report.logs)
    result.attempted = True

    coroutine = _run_in_browser(
        candidate_url,
        download_path,
        browser=browser,
        selectors=selectors_to_try,
        headless=headless,
        login_wait_seconds=login_wait_seconds,
        click_timeout_seconds=click_timeout_seconds,
        result=result,
    )

    try:
        path = asyncio.run(coroutine)
    except RuntimeError as runtime_error:  # pragma: no cover - nested loop fallback
        if "event loop" in str(runtime_error).lower():
            loop = asyncio.new_event_loop()
            try:
                path = loop.run_until_complete(coroutine)
            finally:
                loop.close()
        else:
            raise

    result.path = path
    return result


def ensure_playwright_setup(browser: str = "chromium") -> SetupReport:
    """Public helper to prepare the automation dependencies."""

    report = SetupReport(succeeded=False)
    if _ensure_playwright_setup(browser, report, install_browser=True):
        report.succeeded = True
    return report


__all__ = [
    "AutomationResult",
    "SetupReport",
    "attempt_automated_pdf_download",
    "ensure_playwright_setup",
]
