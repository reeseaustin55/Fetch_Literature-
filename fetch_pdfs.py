#!/usr/bin/env python3
"""GUI tool to download PDFs for bibliography entries via Selenium-controlled browsers."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

import re
import unicodedata
import sys
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

import importlib.util
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


selenium_spec = importlib.util.find_spec("selenium")
if selenium_spec is not None:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
else:  # pragma: no cover - executed only when selenium is unavailable
    webdriver = None  # type: ignore
    TimeoutException = WebDriverException = Exception  # type: ignore


pypdf2_spec = importlib.util.find_spec("PyPDF2")
if pypdf2_spec is not None:
    from PyPDF2 import PdfMerger  # type: ignore
else:  # pragma: no cover - executed only when PyPDF2 is unavailable
    PdfMerger = None  # type: ignore


REFERENCE_LEAD_PATTERN = re.compile(r"^\s*(?:\[\d+\]|\(\d+\)|\d+[.)])\s*")
LOOSE_REFERENCE_LEAD_PATTERN = re.compile(
    r"^\s*\d+\s+(?=.*[A-Za-zÀ-ÖØ-öø-ÿ])"
)
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
PAGES_PATTERN = re.compile(r"\b(\d{1,4}\s*[–-]\s*\d{1,4})\b")
JOURNAL_BEFORE_YEAR_PATTERN = re.compile(
    r"[,;]\s*([^,;]+?)(?=[,;]\s*(?:19|20)\d{2}\b)"
)

CHALLENGE_KEYWORDS = (
    "i'm not a robot",
    "not a robot",
    "unusual traffic",
    "recaptcha",
    "press and hold",
    "complete the captcha",
)


SCHOLAR_BASE_URL = "https://scholar.google.com"

SCHOLAR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0 Safari/537.36"
)

SCHOLAR_PDF_LINK_PATTERN = re.compile(
    r"<div class=\"gs_or_ggsm\".*?<a href=\"([^\"]+)\"",
    re.IGNORECASE | re.DOTALL,
)

SCHOLAR_ARTICLE_LINK_PATTERN = re.compile(
    r"<h3 class=\"gs_rt\".*?<a href=\"([^\"]+)\"",
    re.IGNORECASE | re.DOTALL,
)

SCHOLAR_TITLE_HTML_PATTERN = re.compile(
    r"<h3 class=\"gs_rt\"[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL
)

HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


PDF_MERGER_AVAILABLE = PdfMerger is not None


def page_requires_verification(page_source: str, current_url: str = "") -> bool:
    """Best-effort detection for verification/captcha interruptions."""

    source_lower = page_source.lower()
    if any(keyword in source_lower for keyword in CHALLENGE_KEYWORDS):
        return True

    url_lower = current_url.lower()
    if "sorry/index" in url_lower and "scholar.google" in url_lower:
        return True

    return False


def _strip_reference_lead(text: str) -> str:
    """Remove numbering prefixes such as "[2]", "3.", or "4 Authors"."""

    stripped = REFERENCE_LEAD_PATTERN.sub("", text, count=1).strip()
    stripped = LOOSE_REFERENCE_LEAD_PATTERN.sub("", stripped, count=1).strip()
    return stripped


def extract_references(text: str) -> List[str]:
    """Split raw bibliography text into distinct references.

    The parser groups contiguous non-empty lines, but also treats new numbering tokens
    (e.g. "[12]", "(3)", "4.", or "5 Authors...") as the start of a fresh reference
    even when references are provided without blank lines between them. Leading
    numbering markers are stripped from the resulting reference text to improve
    search results.
    """

    references: List[str] = []
    current: List[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                references.append(" ".join(current))
                current = []
            continue

        lead_match = REFERENCE_LEAD_PATTERN.match(stripped)
        loose_match = None
        if not lead_match:
            loose_match = LOOSE_REFERENCE_LEAD_PATTERN.match(stripped)

        if lead_match or loose_match:
            if current:
                references.append(" ".join(current))
                current = []
            stripped = _strip_reference_lead(stripped)

        current.append(stripped)

    if current:
        references.append(" ".join(current))

    return references


INVALID_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    without_marks = "".join(
        ch for ch in normalized if not unicodedata.combining(ch)
    ).strip()
    sanitized = INVALID_FILENAME_CHARS.sub("_", without_marks)
    sanitized = re.sub(r"_+", "_", sanitized).strip("._")
    return sanitized


YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")
TITLE_PATTERN = re.compile(r"\.\s+([A-Z][^.]+?)\.\s+[A-Z]")


def derive_title(reference: str) -> str:
    """Attempt to extract the study title from a reference string."""

    cleaned = _strip_reference_lead(reference)
    if not cleaned:
        return ""

    pre_url = cleaned.split("http", 1)[0].strip()
    search_scope = pre_url

    match = TITLE_PATTERN.search(pre_url)
    if match:
        candidate = match.group(1).strip()
    else:
        year_match = YEAR_PATTERN.search(pre_url)
        if year_match:
            search_scope = pre_url[: year_match.start()].rstrip("., ;")
        else:
            search_scope = pre_url

        segments = [
            segment.strip() for segment in search_scope.split(". ") if segment.strip()
        ]

        candidate = ""
        for segment in segments:
            if segment.count(";") >= 1 and segment.count(" ") <= 3:
                continue
            if len(segment.split()) >= 3:
                candidate = segment
                break

        if not candidate:
            candidate = segments[0] if segments else pre_url

        candidate = candidate.strip(" .;:,-")

    return candidate


def _extract_first_author(reference: str) -> str:
    cleaned = _strip_reference_lead(reference)
    if not cleaned:
        return ""

    first_segment = cleaned.split(",", 1)[0].strip()
    if not first_segment:
        return ""

    match = re.search(r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`-]+)$", first_segment)
    if match:
        return match.group(1)
    return ""


def _extract_journal(reference: str) -> str:
    cleaned = _strip_reference_lead(reference)
    if not cleaned:
        return ""

    cleaned = DOI_PATTERN.sub("", cleaned)
    cleaned = URL_PATTERN.sub("", cleaned)

    match = JOURNAL_BEFORE_YEAR_PATTERN.search(cleaned)
    if match:
        journal = match.group(1).strip(" .,;:()")
        return journal
    return ""


def build_search_query(reference: str) -> str:
    cleaned = _strip_reference_lead(reference)
    if not cleaned:
        return reference

    doi_match = DOI_PATTERN.search(cleaned)
    if doi_match:
        return _strip_trailing_punctuation(doi_match.group(0))

    url_match = URL_PATTERN.search(cleaned)
    if url_match:
        return _strip_trailing_punctuation(url_match.group(0))

    components: List[str] = []

    normalized_reference = re.sub(r"\s+", " ", cleaned).strip()
    if normalized_reference:
        components.append(normalized_reference)

    title = derive_title(reference).strip()
    title_is_valid = (
        bool(title)
        and len(title) >= 8
        and "," not in title
        and not re.match(r"^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`-]+,?\s*[A-Z]?\.?$", title)
    )
    if title_is_valid:
        components.append(title)

    journal = _extract_journal(reference)
    if journal and (not title_is_valid or journal.lower() not in title.lower()):
        components.append(journal)

    year_match = YEAR_PATTERN.search(cleaned)
    if year_match:
        components.append(year_match.group(0))

    pages_match = PAGES_PATTERN.search(cleaned)
    if pages_match:
        page_token = pages_match.group(1).replace(" ", "")
        components.append(page_token)

    author_last_name = _extract_first_author(reference)
    if author_last_name:
        components.append(author_last_name)

    if not components:
        return cleaned

    return " ".join(dict.fromkeys(components))


TRAILING_PUNCTUATION = ".,);:]\"'"


def _strip_trailing_punctuation(value: str) -> str:
    return value.rstrip(TRAILING_PUNCTUATION)


def build_reference_signature(reference: str, title: str = "") -> Optional[str]:
    """Generate a normalized token used to detect duplicate references."""

    cleaned = _strip_reference_lead(reference)
    if not cleaned:
        return None

    doi_match = DOI_PATTERN.search(cleaned)
    if doi_match:
        return f"doi:{_strip_trailing_punctuation(doi_match.group(0)).lower()}"

    url_match = URL_PATTERN.search(cleaned)
    if url_match:
        return f"url:{_strip_trailing_punctuation(url_match.group(0)).lower()}"

    normalized_title = re.sub(r"\s+", " ", title).strip().lower()
    if normalized_title:
        return f"title:{normalized_title}"

    normalized_reference = re.sub(r"\s+", " ", cleaned).strip().lower()
    return normalized_reference or None


@dataclass
class DownloadResult:
    target: str
    success: bool
    message: str
    destination: Optional[Path] = None
    used_filename: Optional[str] = None


@dataclass
class ReferenceTask:
    index: int
    reference: str
    output_name: str
    preview: str
    duplicate_of: Optional[int] = None


@dataclass
class ManualTargets:
    query_url: str
    article_url: Optional[str]
    pdf_url: Optional[str]
    title: Optional[str] = None


def create_failure_report(destination: Path, failures: List["DownloadResult"]) -> Optional[Path]:
    """Write a text document listing references that did not download."""

    if not failures:
        return None

    destination.mkdir(parents=True, exist_ok=True)
    report_path = _dedupe_path(destination / "missing_pdfs.txt")

    lines: List[str] = []
    for idx, failure in enumerate(failures, start=1):
        lines.append(f"{idx}. {failure.target}")
        if failure.message:
            lines.append(f"   Reason: {failure.message}")
        lines.append("")

    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return report_path


def _dedupe_path(path: Path) -> Path:
    counter = 1
    final_path = path
    while final_path.exists():
        final_path = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    return final_path


def stitch_pdfs(
    pdf_files: List[Path], destination_dir: Path
) -> Tuple[Optional[Path], Optional[str]]:
    """Combine multiple PDF files into a single document.

    Returns a tuple of the merged file path (if created) and a message describing
    why the merge was skipped or failed. When no PDFs are supplied the function
    returns ``(None, None)``.
    """

    valid_files = [path for path in pdf_files if path.exists()]
    if not valid_files:
        return None, None

    if not PDF_MERGER_AVAILABLE:
        return None, "Install PyPDF2 to enable combined PDF output."

    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Failed to prepare destination for combined PDF ({exc})"
    merged_path = _dedupe_path(destination_dir / "combined_references.pdf")

    merger = PdfMerger()  # type: ignore[call-arg]
    try:
        for pdf_path in valid_files:
            merger.append(str(pdf_path))
        with merged_path.open("wb") as handle:
            merger.write(handle)
    except Exception as exc:  # pragma: no cover - best effort cleanup
        try:
            merger.close()
        finally:
            if merged_path.exists():
                try:
                    merged_path.unlink()
                except OSError:
                    pass
        return None, f"Failed to create combined PDF ({exc})"

    try:
        merger.close()
    except Exception:  # pragma: no cover - merger cleanup failures
        pass

    return merged_path, None


def get_default_download_dir() -> Path:
    """Best-effort guess of the user's default download directory."""

    if sys.platform.startswith("win"):
        return Path.home() / "Downloads"
    if sys.platform == "darwin":
        return Path.home() / "Downloads"
    # Assume XDG-style layout for Linux/Unix platforms.
    return Path.home() / "Downloads"


def wait_for_manual_pdf(
    download_dir: Path,
    existing_files: set[Path],
    timeout: float,
    skip_event: Optional[threading.Event] = None,
) -> Optional[Path]:
    """Poll the user's download directory for a new PDF file."""

    deadline = time.time() + max(timeout, 1.0)
    resolved_existing = {p.resolve() for p in existing_files}
    size_tracker: dict[Path, int] = {}
    stable_counts: dict[Path, int] = {}

    while time.time() < deadline:
        if skip_event is not None and skip_event.is_set():
            raise SkipRequested()

        for candidate in download_dir.glob("*.pdf"):
            try:
                resolved = candidate.resolve()
            except FileNotFoundError:
                continue
            if resolved in resolved_existing:
                continue
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            previous_size = size_tracker.get(resolved)
            if previous_size is not None and size == previous_size:
                stable_counts[resolved] = stable_counts.get(resolved, 0) + 1
            else:
                stable_counts[resolved] = 0
            size_tracker[resolved] = size
            if stable_counts[resolved] >= 1:
                return resolved

        time.sleep(1)

    return None


def _merge_failure_messages(initial: str, retry: str) -> str:
    parts: List[str] = []
    if initial:
        parts.append(f"initial attempt: {initial}")
    if retry:
        parts.append(f"retry attempt: {retry}")
    return "; ".join(parts) if parts else ""


def _make_absolute_url(url: str, base: str) -> str:
    if not url:
        return url
    lowered = url.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return url
    if lowered.startswith("//"):
        return f"https:{url}" if not lowered.startswith("https://") else url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return url


def _parse_manual_targets(
    html: str, base: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    pdf_url: Optional[str] = None
    article_url: Optional[str] = None
    title: Optional[str] = None

    pdf_match = SCHOLAR_PDF_LINK_PATTERN.search(html)
    if pdf_match:
        pdf_url = _make_absolute_url(unescape(pdf_match.group(1).strip()), base)

    article_match = SCHOLAR_ARTICLE_LINK_PATTERN.search(html)
    if article_match:
        article_url = _make_absolute_url(unescape(article_match.group(1).strip()), base)

    title_match = SCHOLAR_TITLE_HTML_PATTERN.search(html)
    if title_match:
        fragment = unescape(title_match.group(1))
        fragment = HTML_TAG_PATTERN.sub(" ", fragment)
        fragment = re.sub(r"\s+", " ", fragment).strip()
        while True:
            cleaned = re.sub(r"^\[[^\]]+\]\s*", "", fragment).strip()
            if cleaned == fragment:
                break
            fragment = cleaned
        title = fragment or None

    return pdf_url, article_url, title


def resolve_manual_targets(reference: str, timeout: float = 10.0) -> ManualTargets:
    query = build_search_query(reference)
    if not query.strip():
        query = reference
    query_url = (
        f"{SCHOLAR_BASE_URL}/scholar?hl=en&as_sdt=0%2C5&q={quote_plus(query)}"
    )

    try:
        request = Request(query_url, headers={"User-Agent": SCHOLAR_USER_AGENT})
        with urlopen(request, timeout=timeout) as response:
            html_bytes = response.read()
    except Exception:
        return ManualTargets(query_url, None, None)

    html_text = html_bytes.decode("utf-8", errors="ignore")
    if page_requires_verification(html_text, query_url):
        return ManualTargets(query_url, None, None)

    pdf_url, article_url, title = _parse_manual_targets(html_text, SCHOLAR_BASE_URL)
    return ManualTargets(query_url, article_url, pdf_url, title)


class SkipRequested(Exception):
    """Raised when the user requests to skip the current download."""


class PDFDownloader:
    """Handles Selenium browser automation to download PDF files via Google Scholar."""

    SCHOLAR_URL = "https://scholar.google.com/"
    CHALLENGE_TIMEOUT = 180

    def __init__(
        self,
        download_dir: Path,
        challenge_callback: Optional[Callable[[str], None]] = None,
        download_timeout: float = 30.0,
        browser_choice: str = "firefox",
    ) -> None:
        if webdriver is None:
            raise RuntimeError(
                "Selenium is not available. Please install it via 'pip install selenium' "
                "and ensure a supported browser driver (geckodriver for Firefox or "
                "chromedriver for Chrome) is installed."
            )
        self.download_dir = download_dir
        self.browser_choice = browser_choice.lower() if browser_choice else "firefox"
        if self.browser_choice not in {"firefox", "chrome"}:
            self.browser_choice = "firefox"
        self.browser_label = "Chrome" if self.browser_choice == "chrome" else "Firefox"
        self.driver = self._create_driver(download_dir, self.browser_choice)
        self.base_handle = self.driver.current_window_handle
        self.challenge_callback = challenge_callback
        self.download_timeout = max(1.0, float(download_timeout))

    @staticmethod
    def _create_driver(download_dir: Path, browser_choice: str) -> "webdriver.Remote":
        if browser_choice == "chrome":
            options = ChromeOptions()
            prefs = {
                "download.default_directory": str(download_dir),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "plugins.always_open_pdf_externally": True,
            }
            options.add_experimental_option("prefs", prefs)
            options.add_argument("--start-maximized")

            driver = webdriver.Chrome(options=options)
        else:
            options = FirefoxOptions()
            options.set_preference("browser.download.folderList", 2)
            options.set_preference("browser.download.dir", str(download_dir))
            options.set_preference(
                "browser.helperApps.neverAsk.saveToDisk", "application/pdf"
            )
            options.set_preference("pdfjs.disabled", True)
            options.set_preference("browser.download.manager.showWhenStarting", False)
            options.set_preference("browser.download.useDownloadDir", True)

            driver = webdriver.Firefox(options=options)

        driver.maximize_window()
        return driver

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass

    def download(
        self,
        reference: str,
        output_name: str,
        final_dir: Path,
        skip_event: Optional[threading.Event] = None,
    ) -> DownloadResult:
        existing_files = {p for p in self.download_dir.iterdir() if p.is_file()}
        try:
            self._search_reference(reference, skip_event)
        except SkipRequested:
            return DownloadResult(reference, False, "Skipped by user")
        except WebDriverException as exc:  # pragma: no cover - runtime protection
            return DownloadResult(reference, False, f"Failed to open Google Scholar: {exc}")

        try:
            result_block = self._get_first_result(skip_event)
        except SkipRequested:
            return DownloadResult(reference, False, "Skipped by user")
        except TimeoutException:
            return DownloadResult(reference, False, "No Google Scholar results were found")

        result_title = self._extract_result_title_text(result_block)

        try:
            pdf_link = self._extract_pdf_link(result_block)
        except WebDriverException as exc:
            return DownloadResult(reference, False, f"Unable to inspect the first result: {exc}")

        article_handle: Optional[str] = None
        article_opened_in_new_tab = False

        if pdf_link is None:
            try:
                article_link = result_block.find_element(By.CSS_SELECTOR, "h3 a")
            except NoSuchElementException:
                return DownloadResult(
                    reference, False, "First result is missing an article link to follow"
                )

            try:
                article_handle, article_opened_in_new_tab = self._open_article_link(
                    article_link, skip_event
                )
            except SkipRequested:
                return DownloadResult(reference, False, "Skipped by user")

            try:
                pdf_link = self._wait_for_pdf_link(skip_event)
            except SkipRequested:
                return DownloadResult(reference, False, "Skipped by user")
            except TimeoutException:
                return DownloadResult(
                    reference,
                    False,
                    "Could not locate a PDF link on the article page",
                )

        if pdf_link is None:
            return DownloadResult(
                reference,
                False,
                "First result did not provide a downloadable PDF",
            )

        try:
            self.driver.execute_script("arguments[0].click();", pdf_link)
        except WebDriverException as exc:
            return DownloadResult(reference, False, f"Failed to trigger PDF download: {exc}")

        try:
            downloaded = self._wait_for_new_file(existing_files, skip_event)
        except SkipRequested:
            return DownloadResult(reference, False, "Skipped by user")
        if downloaded is None:
            return DownloadResult(reference, False, "Download did not complete in time")

        try:
            downloaded_size = downloaded.stat().st_size
        except OSError as exc:
            try:
                downloaded.unlink()
            except OSError:
                pass
            return DownloadResult(
                reference,
                False,
                f"Could not read downloaded PDF ({exc})",
            )

        if downloaded_size == 0:
            try:
                downloaded.unlink()
            except OSError:
                pass
            return DownloadResult(
                reference,
                False,
                "Downloaded PDF was empty",
            )

        if article_opened_in_new_tab and article_handle:
            self._close_tab(article_handle)

        self._close_extra_tabs()

        sanitized_title = _sanitize_filename(result_title)[:150] if result_title else ""
        final_name = sanitized_title if sanitized_title else output_name

        destination = final_dir / f"{final_name}.pdf"
        destination = self._dedupe_destination(destination)
        shutil.move(str(downloaded), destination)
        return DownloadResult(
            reference,
            True,
            "Downloaded",
            destination,
            used_filename=destination.stem,
        )

    def _search_reference(
        self, reference: str, skip_event: Optional[threading.Event] = None
    ) -> None:
        query = build_search_query(reference)
        if not query.strip():
            query = reference

        try:
            self.driver.switch_to.window(self.base_handle)
        except WebDriverException:
            pass
        self.driver.get(self.SCHOLAR_URL)
        self._handle_challenge(
            "Google Scholar asked for verification before the search box became available.",
            skip_event,
        )

        def locate_box(driver):
            self._check_skip(skip_event)
            return driver.find_element(By.NAME, "q")

        wait = WebDriverWait(self.driver, 20)
        search_box = wait.until(locate_box)
        search_box.clear()
        search_box.send_keys(query)
        search_box.submit()
        self._handle_challenge(
            "Google Scholar requested verification after submitting the query.",
            skip_event,
        )

    def _get_first_result(self, skip_event: Optional[threading.Event] = None):
        deadline = time.time() + 60
        last_exception: Optional[Exception] = None
        while time.time() < deadline:
            self._check_skip(skip_event)
            self._handle_challenge(
                "Google Scholar needs verification before showing the search results.",
                skip_event,
            )
            wait = WebDriverWait(self.driver, 10)
            try:
                return wait.until(
                    lambda drv: self._locate_first_result(drv, skip_event)
                )
            except TimeoutException as exc:
                last_exception = exc
        if last_exception:
            raise last_exception
        raise TimeoutException("No Google Scholar results were found")

    def _locate_first_result(
        self, driver, skip_event: Optional[threading.Event]
    ):
        self._check_skip(skip_event)
        elements = driver.find_elements(By.CSS_SELECTOR, "div.gs_r.gs_or.gs_scl")
        return elements[0] if elements else False

    @staticmethod
    def _extract_pdf_link(result_block):
        try:
            return result_block.find_element(By.CSS_SELECTOR, "div.gs_or_ggsm a")
        except NoSuchElementException:
            return None

    @staticmethod
    def _extract_result_title_text(result_block) -> str:
        try:
            title_element = result_block.find_element(By.CSS_SELECTOR, "h3")
        except NoSuchElementException:
            return ""

        raw_text = (title_element.text or "").strip()
        if not raw_text:
            raw_text = (title_element.get_attribute("textContent") or "").strip()

        normalized = re.sub(r"\s+", " ", raw_text).strip()
        while True:
            cleaned = re.sub(r"^\[[^\]]+\]\s*", "", normalized).strip()
            if cleaned == normalized:
                break
            normalized = cleaned

        return normalized

    def _open_article_link(
        self, link, skip_event: Optional[threading.Event] = None
    ):
        existing_handles = set(self.driver.window_handles)
        try:
            self.driver.execute_script("arguments[0].click();", link)
        except WebDriverException:
            link.click()

        new_handle = self._wait_for_new_window(existing_handles, skip_event)
        if new_handle:
            self.driver.switch_to.window(new_handle)
            return new_handle, True
        return self.driver.current_window_handle, False

    def _wait_for_pdf_link(
        self, skip_event: Optional[threading.Event] = None
    ):
        wait = WebDriverWait(self.driver, 20, poll_frequency=0.5)

        def locate(driver):
            self._check_skip(skip_event)
            candidates = driver.find_elements(
                By.XPATH,
                "//a[contains(@href, '.pdf') or contains(translate(text(), 'pdf', 'PDF'), 'PDF')]",
            )
            for candidate in candidates:
                try:
                    if candidate.is_enabled() and candidate.is_displayed():
                        return candidate
                except WebDriverException:
                    continue
            return False

        return wait.until(locate)

    def _wait_for_new_file(
        self, existing_files: set[Path], skip_event: Optional[threading.Event] = None
    ) -> Optional[Path]:
        timeout = time.time() + self.download_timeout
        while time.time() < timeout:
            self._check_skip(skip_event)
            current_files = {p for p in self.download_dir.iterdir() if p.is_file()}
            new_files = current_files - existing_files
            for candidate in new_files:
                if candidate.suffix.lower() == ".pdf" and not candidate.name.endswith(".part"):
                    return candidate
            time.sleep(1)
        return None

    def _wait_for_new_window(
        self, existing_handles: set[str], skip_event: Optional[threading.Event] = None
    ) -> Optional[str]:
        end = time.time() + 10
        while time.time() < end:
            self._check_skip(skip_event)
            handles = set(self.driver.window_handles)
            new_handles = handles - existing_handles
            if new_handles:
                return new_handles.pop()
            time.sleep(0.5)
        return None

    def _close_tab(self, handle: str) -> None:
        try:
            self.driver.switch_to.window(handle)
            self.driver.close()
        except WebDriverException:
            pass
        finally:
            try:
                self.driver.switch_to.window(self.base_handle)
            except WebDriverException:
                pass

    def _close_extra_tabs(self) -> None:
        for handle in list(self.driver.window_handles):
            if handle == self.base_handle:
                continue
            self._close_tab(handle)
        try:
            self.driver.switch_to.window(self.base_handle)
        except WebDriverException:
            pass

    @staticmethod
    def _dedupe_destination(path: Path) -> Path:
        return _dedupe_path(path)

    def _handle_challenge(
        self, context: str, skip_event: Optional[threading.Event] = None
    ) -> None:
        if not self._is_challenge_page():
            return

        message = (
            "Google Scholar is requesting verification (e.g., an 'I'm not a robot' "
            "challenge). "
            f"{context}"
            "\n\nPlease switch to the "
            f"{self.browser_label}"
            " window, complete the verification, and then return to this application."
        )
        if self.challenge_callback is not None:
            try:
                self.challenge_callback(message)
            except Exception:
                pass

        deadline = time.time() + self.CHALLENGE_TIMEOUT
        while time.time() < deadline:
            self._check_skip(skip_event)
            time.sleep(1)
            if not self._is_challenge_page():
                return

        raise TimeoutException("Verification challenge was not cleared in time")

    @staticmethod
    def _check_skip(skip_event: Optional[threading.Event]) -> None:
        if skip_event is not None and skip_event.is_set():
            raise SkipRequested()

    def _is_challenge_page(self) -> bool:
        try:
            source = self.driver.page_source
            current_url = self.driver.current_url
        except WebDriverException:
            return False

        return page_requires_verification(source, current_url)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fetch Bibliography PDFs")
        self.geometry("700x500")
        self.minsize(500, 400)
        self.resizable(True, True)
        self.download_thread: Optional[threading.Thread] = None
        self.downloader: Optional[PDFDownloader] = None
        self._skip_event = threading.Event()
        self.browser_var = tk.StringVar(value="Firefox")
        self.manual_retry_var = tk.BooleanVar(value=True)
        self.manual_auto_var = tk.BooleanVar(value=True)
        self._manual_prompt_acknowledged = False
        self._current_browser_label = "Firefox"
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        instruction = tk.Label(
            self, text="Paste your bibliography entries below (one per line):"
        )
        instruction.grid(row=0, column=0, sticky="w", padx=10, pady=(10, 0))

        self.text_box = scrolledtext.ScrolledText(self, wrap=tk.WORD)
        self.text_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)

        path_frame = tk.Frame(self)
        path_frame.grid(row=2, column=0, sticky="ew", padx=10)
        path_frame.columnconfigure(1, weight=1)

        tk.Label(path_frame, text="Destination folder:").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar(value=str(Path.home() / "Desktop" / "Bibliography_PDFs"))
        self.path_entry = tk.Entry(path_frame, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        tk.Button(path_frame, text="Browse", command=self._choose_folder).grid(
            row=0, column=2, padx=(0, 5)
        )

        tk.Label(path_frame, text="Download timeout (seconds):").grid(
            row=1, column=0, sticky="w"
        )
        self.timeout_var = tk.StringVar(value="10")
        self.timeout_entry = tk.Entry(path_frame, textvariable=self.timeout_var, width=10)
        self.timeout_entry.grid(row=1, column=1, sticky="w", padx=5, pady=(0, 5))

        tk.Label(path_frame, text="Automated browser:").grid(
            row=2, column=0, sticky="w"
        )
        browser_menu = tk.OptionMenu(path_frame, self.browser_var, "Firefox", "Chrome")
        browser_menu.grid(row=2, column=1, sticky="w", padx=5, pady=(0, 5))

        manual_check = tk.Checkbutton(
            path_frame,
            text="Offer manual browser fallback for missed PDFs",
            variable=self.manual_retry_var,
        )
        manual_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 5))

        auto_manual_check = tk.Checkbutton(
            path_frame,
            text="Try to auto-open the first PDF when manual fallback runs",
            variable=self.manual_auto_var,
        )
        auto_manual_check.grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 5))

        controls_frame = tk.Frame(self)
        controls_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(5, 10))
        controls_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Idle")
        status_label = tk.Label(controls_frame, textvariable=self.status_var, anchor="w")
        status_label.grid(row=0, column=0, sticky="w")

        self.download_button = tk.Button(
            controls_frame, text="Download PDFs", command=self._start_download
        )
        self.download_button.grid(row=0, column=1, padx=(10, 0))

        self.skip_button = tk.Button(
            controls_frame,
            text="Skip ▶",
            command=self._request_skip,
            state=tk.DISABLED,
        )
        self.skip_button.grid(row=0, column=2, padx=(10, 0))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get())
        if selected:
            self.path_var.set(selected)

    def _start_download(self) -> None:
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showinfo(
                "Download in progress", "Please wait for the current download to finish."
            )
            return

        references = extract_references(self.text_box.get("1.0", tk.END))
        if not references:
            messagebox.showwarning(
                "No references", "No bibliography entries were detected in the provided text."
            )
            return

        destination = Path(self.path_var.get()).expanduser()
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Invalid folder", f"Could not create destination: {exc}")
            return

        try:
            timeout_seconds = float(self.timeout_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid timeout",
                "Please enter a numeric timeout value (in seconds).",
            )
            return

        if timeout_seconds <= 0:
            messagebox.showerror(
                "Invalid timeout", "Timeout must be greater than zero seconds."
            )
            return

        browser_choice = self.browser_var.get().strip().lower()
        if browser_choice not in {"firefox", "chrome"}:
            browser_choice = "firefox"
        self._current_browser_label = "Chrome" if browser_choice == "chrome" else "Firefox"

        self.status_var.set("Starting downloads...")
        self.download_button.config(state=tk.DISABLED)
        self.skip_button.config(state=tk.NORMAL)
        self._skip_event.clear()
        self.download_thread = threading.Thread(
            target=self._run_downloads,
            args=(references, destination, timeout_seconds, browser_choice),
            daemon=True,
        )
        self.download_thread.start()

    def _run_downloads(
        self,
        references: List[str],
        destination: Path,
        timeout_seconds: float,
        browser_choice: str,
    ) -> None:
        self._manual_prompt_acknowledged = False
        temp_dir = Path(tempfile.mkdtemp(prefix="fetch_pdfs_"))
        tasks: List[ReferenceTask] = []
        seen_signatures: dict[str, int] = {}
        for index, reference in enumerate(references, start=1):
            title = derive_title(reference)
            sanitized_title = _sanitize_filename(title)[:150] if title else ""
            output_name = sanitized_title if sanitized_title else f"reference_{index:03d}"
            preview = reference if len(reference) <= 80 else reference[:77] + "..."
            signature = build_reference_signature(reference, title)
            duplicate_of = None
            if signature:
                if signature in seen_signatures:
                    duplicate_of = seen_signatures[signature]
                else:
                    seen_signatures[signature] = index
            tasks.append(
                ReferenceTask(index, reference, output_name, preview, duplicate_of)
            )

        final_results: List[Optional[DownloadResult]] = [None] * len(tasks)
        retry_successes: List[ReferenceTask] = []
        retry_candidates: List[tuple[int, ReferenceTask, DownloadResult]] = []
        manual_successes: List[ReferenceTask] = []

        try:
            self.downloader = PDFDownloader(
                temp_dir,
                self._prompt_challenge,
                download_timeout=timeout_seconds,
                browser_choice=browser_choice,
            )
            total_tasks = len(references)
            for task in tasks:
                if task.duplicate_of is not None:
                    self._update_status(
                        f"Skipping duplicate {task.index}/{total_tasks}: matches entry {task.duplicate_of}"
                    )
                    original_result = (
                        final_results[task.duplicate_of - 1]
                        if 0 <= task.duplicate_of - 1 < len(final_results)
                        else None
                    )
                    if isinstance(original_result, DownloadResult):
                        if original_result.success:
                            message = (
                                f"Duplicate of entry {task.duplicate_of}; reused downloaded file"
                            )
                        else:
                            reason = (
                                f"{original_result.message}"
                                if original_result.message
                                else "original attempt failed"
                            )
                            message = (
                                f"Duplicate of entry {task.duplicate_of}; original failed: {reason}"
                            )
                        final_results[task.index - 1] = DownloadResult(
                            task.reference,
                            original_result.success,
                            message,
                            original_result.destination,
                            used_filename=original_result.used_filename,
                        )
                    else:
                        final_results[task.index - 1] = DownloadResult(
                            task.reference,
                            False,
                            f"Duplicate of entry {task.duplicate_of}; original result unavailable",
                        )
                    continue

                self._update_status(
                    f"Processing {task.index}/{total_tasks}: {task.preview}"
                )
                result = self.downloader.download(
                    task.reference,
                    task.output_name,
                    destination,
                    skip_event=self._skip_event,
                )
                self._skip_event.clear()
                final_results[task.index - 1] = result
                if not result.success:
                    retry_candidates.append((task.index - 1, task, result))

            if retry_candidates:
                self._update_status(
                    f"Retrying {len(retry_candidates)} reference(s) that failed initially..."
                )
                for slot, task, first_result in retry_candidates:
                    if task.duplicate_of is not None:
                        continue
                    self._update_status(
                        f"Retrying {task.index}/{len(references)}: {task.preview}"
                    )
                    retry_result = self.downloader.download(
                        task.reference,
                        task.output_name,
                        destination,
                        skip_event=self._skip_event,
                    )
                    self._skip_event.clear()
                    if retry_result.success:
                        retry_result.message = "Downloaded on retry"
                        final_results[slot] = retry_result
                        retry_successes.append(task)
                    else:
                        retry_result.message = _merge_failure_messages(
                            first_result.message, retry_result.message
                        )
                        final_results[slot] = retry_result

            if self.manual_retry_var.get():
                manual_timeout = 20.0
                manual_successes = self._run_manual_fallback(
                    tasks,
                    final_results,
                    destination,
                    manual_timeout,
                )
        except Exception as exc:  # pragma: no cover - GUI feedback only
            error_message = str(exc)
            for idx, value in enumerate(final_results):
                if isinstance(value, DownloadResult):
                    continue
                reference = tasks[idx].reference if idx < len(tasks) else "Initialization"
                final_results[idx] = DownloadResult(reference, False, error_message)
            if not final_results:
                final_results = [DownloadResult("Initialization", False, error_message)]
            retry_successes = []
        finally:
            if self.downloader:
                self.downloader.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

        results: List[DownloadResult]
        if final_results and all(isinstance(r, DownloadResult) for r in final_results):
            results = [r for r in final_results if isinstance(r, DownloadResult)]
        else:
            results = [DownloadResult("Initialization", False, "Downloader did not start")]  # pragma: no cover

        success = sum(1 for r in results if r.success)
        unique_failures: List[DownloadResult] = []
        for task, result in zip(tasks, results):
            if result.success:
                continue
            if task.duplicate_of is not None:
                continue
            unique_failures.append(result)
        success_paths = [
            r.destination
            for r in results
            if r.success and r.destination is not None and r.destination.exists()
        ]

        summary_lines = [f"Downloaded {success} of {len(references)} references."]
        if retry_successes:
            summary_lines.append(
                f"{len(retry_successes)} reference(s) succeeded on a second attempt."
            )
        if manual_successes:
            summary_lines.append(
                f"{len(manual_successes)} reference(s) were completed via manual fallback."
            )
        for fail in unique_failures:
            summary_lines.append(f"- {fail.target}: {fail.message}")

        combined_path, combine_message = stitch_pdfs(success_paths, destination)
        report_path = create_failure_report(destination, unique_failures)

        notes: List[str] = []
        if combined_path:
            notes.append(f"Combined PDF saved to: {combined_path}")
        elif combine_message:
            notes.append(combine_message)

        if report_path:
            notes.append(f"A list of missed PDFs was saved to: {report_path}")

        if notes:
            summary_lines.append("")
            summary_lines.extend(notes)

        self._finish(summary_lines)

    def _run_manual_fallback(
        self,
        tasks: List[ReferenceTask],
        final_results: List[Optional[DownloadResult]],
        destination: Path,
        timeout_seconds: float,
    ) -> List[ReferenceTask]:
        pending: List[tuple[int, ReferenceTask, DownloadResult]] = []
        for idx, result in enumerate(final_results):
            task = tasks[idx]
            if task.duplicate_of is not None:
                continue
            if isinstance(result, DownloadResult) and not result.success:
                pending.append((idx, task, result))

        if not pending:
            return []

        downloads_dir = get_default_download_dir()
        try:
            downloads_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            note = f"Manual fallback unavailable ({exc})"
            for idx, _task, previous in pending:
                final_results[idx] = DownloadResult(
                    previous.target,
                    False,
                    self._append_manual_note(previous.message, note),
                )
            self._update_status("Manual fallback could not start (downloads folder missing).")
            return []

        manual_successes: List[ReferenceTask] = []
        total = len(tasks)
        for idx, task, previous in pending:
            self._update_status(
                f"Manual fallback for {task.index}/{total}: {task.preview}"
            )
            manual_result = self._perform_manual_download(
                task,
                destination,
                downloads_dir,
                timeout_seconds,
                previous.message,
            )
            self._skip_event.clear()
            final_results[idx] = manual_result
            if manual_result.success:
                manual_successes.append(task)

        if manual_successes:
            self._update_status("Manual fallback completed.")

        return manual_successes

    def _perform_manual_download(
        self,
        task: ReferenceTask,
        destination: Path,
        downloads_dir: Path,
        timeout_seconds: float,
        previous_message: str,
    ) -> DownloadResult:
        proceed: bool
        if self._manual_prompt_acknowledged:
            proceed = True
        else:
            proceed = self._prompt_manual_confirmation(
                task, downloads_dir, timeout_seconds
            )
            if proceed:
                self._manual_prompt_acknowledged = True
        if not proceed:
            return DownloadResult(
                task.reference,
                False,
                self._append_manual_note(
                    previous_message, "Manual fallback skipped by user"
                ),
            )

        targets = resolve_manual_targets(task.reference)
        auto_notes: List[str] = []
        opened = False

        if self.manual_auto_var.get():
            auto_targets: List[Tuple[str, str]] = []
            if targets.pdf_url:
                auto_targets.append(("PDF", targets.pdf_url))
            if targets.article_url and targets.article_url != targets.pdf_url:
                auto_targets.append(("article", targets.article_url))

            for label, url in auto_targets:
                self._update_status(
                    f"Opening detected {label.lower()} link in default browser..."
                )
                try:
                    webbrowser.open_new_tab(url)
                    opened = True
                    auto_notes.append(f"Opened {label.lower()} link automatically")
                except Exception as exc:  # pragma: no cover - OS/browser specific
                    auto_notes.append(
                        f"Automatic {label.lower()} open failed ({exc})"
                    )

        if not opened:
            try:
                webbrowser.open_new_tab(targets.query_url)
            except Exception as exc:  # pragma: no cover - OS/browser specific
                return DownloadResult(
                    task.reference,
                    False,
                    self._append_manual_note(
                        previous_message, f"Could not open browser ({exc})"
                    ),
                )

        if auto_notes:
            previous_message = self._append_manual_note(
                previous_message, "; ".join(auto_notes)
            )

        existing_files = {p.resolve() for p in downloads_dir.glob("*.pdf")}
        self._update_status(
            f"Waiting for manual download in {downloads_dir}: {task.preview}"
        )

        try:
            manual_file = wait_for_manual_pdf(
                downloads_dir,
                existing_files,
                timeout_seconds,
                skip_event=self._skip_event,
            )
        except SkipRequested:
            return DownloadResult(
                task.reference,
                False,
                self._append_manual_note(
                    previous_message, "Skipped during manual fallback"
                ),
            )

        if manual_file is None:
            return DownloadResult(
                task.reference,
                False,
                self._append_manual_note(
                    previous_message, "Manual fallback timed out"
                ),
            )

        try:
            manual_size = manual_file.stat().st_size
        except OSError as exc:
            try:
                manual_file.unlink()
            except OSError:
                pass
            return DownloadResult(
                task.reference,
                False,
                self._append_manual_note(
                    previous_message,
                    f"Manual download could not be read ({exc})",
                ),
            )

        if manual_size == 0:
            try:
                manual_file.unlink()
            except OSError:
                pass
            return DownloadResult(
                task.reference,
                False,
                self._append_manual_note(
                    previous_message, "Manual download appeared empty",
                ),
            )

        preferred_name = task.output_name
        if targets.title:
            sanitized = _sanitize_filename(targets.title)[:150]
            if sanitized:
                preferred_name = sanitized

        destination_path = _dedupe_path(destination / f"{preferred_name}.pdf")
        try:
            shutil.move(str(manual_file), destination_path)
        except OSError as exc:
            return DownloadResult(
                task.reference,
                False,
                self._append_manual_note(
                    previous_message, f"Could not move manual download ({exc})"
                ),
            )

        return DownloadResult(
            task.reference,
            True,
            "Downloaded manually",
            destination_path,
            used_filename=destination_path.stem,
        )

    def _prompt_manual_confirmation(
        self, task: ReferenceTask, downloads_dir: Path, timeout_seconds: float
    ) -> bool:
        event = threading.Event()
        decision = {"proceed": False}

        def prompt() -> None:
            message = (
                "Automated attempts were unable to fetch this reference:\n\n"
                f"{task.reference}\n\n"
                "Click Yes to open the search in your default browser so you can "
                "download the PDF manually. Save the PDF into the folder:\n"
                f"{downloads_dir}\n\n"
                f"The app will watch for a new PDF there for up to {int(timeout_seconds)} seconds."
            )
            decision["proceed"] = messagebox.askyesno(
                "Manual download required", message
            )
            event.set()

        self.after(0, prompt)
        event.wait()
        return decision["proceed"]

    @staticmethod
    def _append_manual_note(previous: str, note: str) -> str:
        return f"{previous}; {note}" if previous else note

    def _update_status(self, text: str) -> None:
        def updater() -> None:
            self.status_var.set(text)

        self.after(0, updater)

    def _finish(self, summary_lines: Iterable[str]) -> None:
        def finish_ui() -> None:
            self.download_button.config(state=tk.NORMAL)
            self.skip_button.config(state=tk.DISABLED)
            self.status_var.set("Done")
            messagebox.showinfo("Download summary", "\n".join(summary_lines))

        self.after(0, finish_ui)

    def _prompt_challenge(self, message: str) -> None:
        event = threading.Event()

        def show_message() -> None:
            browser_label = self._current_browser_label or "your browser"
            self.status_var.set(
                f"Waiting for manual verification in {browser_label} (complete the challenge and click OK)..."
            )
            messagebox.showinfo("Manual verification required", message)
            event.set()

        self.after(0, show_message)
        event.wait()
        self._update_status("Resuming downloads...")

    def _request_skip(self) -> None:
        if not (self.download_thread and self.download_thread.is_alive()):
            return
        self._skip_event.set()
        self._update_status("Skip requested. Moving to the next reference...")


if __name__ == "__main__":
    app = App()
    app.mainloop()
