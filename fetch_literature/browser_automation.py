"""Helpers for driving a browser to fetch bibliography PDFs automatically."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
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
        return AutomationResult(attempted=False, error="No URL provided for automated download.")

    try:
        import importlib

        importlib.import_module("playwright.async_api")
    except ImportError:
        return AutomationResult(
            attempted=False,
            error=(
                "Playwright is not installed. Install it with 'pip install playwright' "
                "and run 'playwright install chromium' to set up the browser runtime."
            ),
        )

    download_path = Path(download_dir or Path.cwd() / "downloads")
    download_path.mkdir(parents=True, exist_ok=True)

    selectors_to_try = list(dict.fromkeys((selectors or []) + list(DEFAULT_SELECTORS)))
    result = AutomationResult(attempted=True)

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


__all__ = ["AutomationResult", "attempt_automated_pdf_download"]
