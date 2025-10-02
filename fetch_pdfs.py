#!/usr/bin/env python3
"""GUI tool to download PDFs for bibliography entries via Selenium-controlled Firefox."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import re
import unicodedata

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

import importlib.util


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
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
else:  # pragma: no cover - executed only when selenium is unavailable
    webdriver = None  # type: ignore
    TimeoutException = WebDriverException = Exception  # type: ignore


REFERENCE_LEAD_PATTERN = re.compile(r"^\s*(?:\[\d+\]|\(\d+\)|\d+\.)\s*")


def extract_references(text: str) -> List[str]:
    """Split raw bibliography text into distinct references.

    The parser groups contiguous non-empty lines, but also treats new numbering tokens
    (e.g. "[12]", "(3)", or "4.") as the start of a fresh reference even when
    references are provided without blank lines between them. Leading numbering
    markers are stripped from the resulting reference text to improve search
    results.
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

        if REFERENCE_LEAD_PATTERN.match(stripped):
            if current:
                references.append(" ".join(current))
                current = []
            stripped = REFERENCE_LEAD_PATTERN.sub("", stripped, count=1).strip()

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

    cleaned = REFERENCE_LEAD_PATTERN.sub("", reference).strip()
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


@dataclass
class DownloadResult:
    target: str
    success: bool
    message: str
    destination: Optional[Path] = None


class PDFDownloader:
    """Handles Selenium browser automation to download PDF files via Google Scholar."""

    SCHOLAR_URL = "https://scholar.google.com/"

    def __init__(self, download_dir: Path) -> None:
        if webdriver is None:
            raise RuntimeError(
                "Selenium is not available. Please install it via 'pip install selenium' "
                "and ensure geckodriver/Firefox are installed."
            )
        self.download_dir = download_dir
        self.driver = self._create_driver(download_dir)
        self.base_handle = self.driver.current_window_handle

    @staticmethod
    def _create_driver(download_dir: Path) -> "webdriver.Firefox":
        options = FirefoxOptions()
        options.set_preference("browser.download.folderList", 2)
        options.set_preference("browser.download.dir", str(download_dir))
        options.set_preference("browser.helperApps.neverAsk.saveToDisk", "application/pdf")
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

    def download(self, reference: str, output_name: str, final_dir: Path) -> DownloadResult:
        existing_files = {p for p in self.download_dir.iterdir() if p.is_file()}
        try:
            self._search_reference(reference)
        except WebDriverException as exc:  # pragma: no cover - runtime protection
            return DownloadResult(reference, False, f"Failed to open Google Scholar: {exc}")

        try:
            result_block = self._get_first_result()
        except TimeoutException:
            return DownloadResult(reference, False, "No Google Scholar results were found")

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

            article_handle, article_opened_in_new_tab = self._open_article_link(article_link)

            try:
                pdf_link = self._wait_for_pdf_link()
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

        downloaded = self._wait_for_new_file(existing_files)
        if downloaded is None:
            return DownloadResult(reference, False, "Download did not complete in time")

        if article_opened_in_new_tab and article_handle:
            self._close_tab(article_handle)

        self._close_extra_tabs()

        destination = final_dir / f"{output_name}.pdf"
        destination = self._dedupe_destination(destination)
        shutil.move(str(downloaded), destination)
        return DownloadResult(reference, True, "Downloaded", destination)

    def _search_reference(self, reference: str) -> None:
        try:
            self.driver.switch_to.window(self.base_handle)
        except WebDriverException:
            pass
        self.driver.get(self.SCHOLAR_URL)
        wait = WebDriverWait(self.driver, 20)
        search_box = wait.until(EC.presence_of_element_located((By.NAME, "q")))
        search_box.clear()
        search_box.send_keys(reference)
        search_box.submit()

    def _get_first_result(self):
        wait = WebDriverWait(self.driver, 20)
        return wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div.gs_r.gs_or.gs_scl")
            )
        )

    @staticmethod
    def _extract_pdf_link(result_block):
        try:
            return result_block.find_element(By.CSS_SELECTOR, "div.gs_or_ggsm a")
        except NoSuchElementException:
            return None

    def _open_article_link(self, link):
        existing_handles = set(self.driver.window_handles)
        try:
            self.driver.execute_script("arguments[0].click();", link)
        except WebDriverException:
            link.click()

        new_handle = self._wait_for_new_window(existing_handles)
        if new_handle:
            self.driver.switch_to.window(new_handle)
            return new_handle, True
        return self.driver.current_window_handle, False

    def _wait_for_pdf_link(self):
        wait = WebDriverWait(self.driver, 20)
        return wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//a[contains(@href, '.pdf') or contains(translate(text(), 'pdf', 'PDF'), 'PDF')]",
                )
            )
        )

    def _wait_for_new_file(self, existing_files: set[Path]) -> Optional[Path]:
        timeout = time.time() + 60
        while time.time() < timeout:
            current_files = {p for p in self.download_dir.iterdir() if p.is_file()}
            new_files = current_files - existing_files
            for candidate in new_files:
                if candidate.suffix.lower() == ".pdf" and not candidate.name.endswith(".part"):
                    return candidate
            time.sleep(1)
        return None

    def _wait_for_new_window(self, existing_handles: set[str]) -> Optional[str]:
        end = time.time() + 10
        while time.time() < end:
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
        counter = 1
        final_path = path
        while final_path.exists():
            final_path = path.with_name(f"{path.stem}_{counter}{path.suffix}")
            counter += 1
        return final_path


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fetch Bibliography PDFs")
        self.geometry("700x500")
        self.minsize(500, 400)
        self.resizable(True, True)
        self.download_thread: Optional[threading.Thread] = None
        self.downloader: Optional[PDFDownloader] = None
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

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get())
        if selected:
            self.path_var.set(selected)

    def _start_download(self) -> None:
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showinfo("Download in progress", "Please wait for current download to finish.")
            return

        references = extract_references(self.text_box.get("1.0", tk.END))
        if not references:
            messagebox.showwarning(
                "No references", "No bibliography entries were detected in the provided text."
            )
            return

        destination = Path(self.path_var.get()).expanduser()
        destination.mkdir(parents=True, exist_ok=True)

        self.status_var.set("Starting downloads...")
        self.download_button.config(state=tk.DISABLED)
        self.download_thread = threading.Thread(
            target=self._run_downloads, args=(references, destination), daemon=True
        )
        self.download_thread.start()

    def _run_downloads(self, references: List[str], destination: Path) -> None:
        temp_dir = Path(tempfile.mkdtemp(prefix="fetch_pdfs_"))
        results: List[DownloadResult] = []
        try:
            self.downloader = PDFDownloader(temp_dir)
            for index, reference in enumerate(references, start=1):
                title = derive_title(reference)
                sanitized_title = _sanitize_filename(title)[:150] if title else ""
                output_name = (
                    sanitized_title if sanitized_title else f"reference_{index:03d}"
                )
                preview = reference if len(reference) <= 80 else reference[:77] + "..."
                self._update_status(f"Processing {index}/{len(references)}: {preview}")
                result = self.downloader.download(reference, output_name, destination)
                results.append(result)
        except Exception as exc:  # pragma: no cover - GUI feedback only
            results.append(DownloadResult("Initialization", False, str(exc)))
        finally:
            if self.downloader:
                self.downloader.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

        success = sum(1 for r in results if r.success)
        failures = [r for r in results if not r.success]

        summary_lines = [f"Downloaded {success} of {len(references)} references."]
        for fail in failures:
            summary_lines.append(f"- {fail.target}: {fail.message}")

        self._finish(summary_lines)

    def _update_status(self, text: str) -> None:
        def updater() -> None:
            self.status_var.set(text)

        self.after(0, updater)

    def _finish(self, summary_lines: Iterable[str]) -> None:
        def finish_ui() -> None:
            self.download_button.config(state=tk.NORMAL)
            self.status_var.set("Done")
            messagebox.showinfo("Download summary", "\n".join(summary_lines))

        self.after(0, finish_ui)


if __name__ == "__main__":
    app = App()
    app.mainloop()
