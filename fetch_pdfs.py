#!/usr/bin/env python3
"""GUI tool to download PDFs for bibliography entries via Selenium-controlled Firefox."""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

import importlib.util


selenium_spec = importlib.util.find_spec("selenium")
if selenium_spec is not None:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
else:  # pragma: no cover - executed only when selenium is unavailable
    webdriver = None  # type: ignore
    TimeoutException = WebDriverException = Exception  # type: ignore


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s)]+", re.IGNORECASE)


def extract_targets(text: str) -> List[str]:
    """Extract DOIs or URLs from the provided text."""
    targets: List[str] = []
    for line in text.splitlines():
        match = DOI_PATTERN.search(line)
        if match:
            doi = match.group(0)
            if not doi.lower().startswith("http"):
                doi = f"https://doi.org/{doi}"
            targets.append(doi)
            continue
        url_match = URL_PATTERN.search(line)
        if url_match:
            targets.append(url_match.group(0).rstrip(".))"))
    return targets


@dataclass
class DownloadResult:
    target: str
    success: bool
    message: str
    destination: Optional[Path] = None


class PDFDownloader:
    """Handles Selenium browser automation to download PDF files."""

    def __init__(self, download_dir: Path) -> None:
        if webdriver is None:
            raise RuntimeError(
                "Selenium is not available. Please install it via 'pip install selenium' "
                "and ensure geckodriver/Firefox are installed."
            )
        self.download_dir = download_dir
        self.driver = self._create_driver(download_dir)

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

    def download(self, target_url: str, output_name: str, final_dir: Path) -> DownloadResult:
        existing_files = {p for p in self.download_dir.iterdir() if p.is_file()}
        try:
            self.driver.get(target_url)
        except WebDriverException as exc:  # pragma: no cover - runtime protection
            return DownloadResult(target_url, False, f"Failed to open page: {exc}")

        try:
            link = self._wait_for_pdf_link()
        except TimeoutException:
            return DownloadResult(target_url, False, "Could not locate a PDF link on the page")

        try:
            pdf_href = link.get_attribute("href") or ""
            if pdf_href.lower().endswith(".pdf"):
                # If direct PDF link, fetch using Selenium to keep authentication context
                self.driver.execute_script("arguments[0].click();", link)
            else:
                link.click()
        except WebDriverException as exc:
            return DownloadResult(target_url, False, f"Failed to trigger download: {exc}")

        downloaded = self._wait_for_new_file(existing_files)
        if downloaded is None:
            return DownloadResult(target_url, False, "Download did not complete in time")

        destination = final_dir / f"{output_name}.pdf"
        destination = self._dedupe_destination(destination)
        shutil.move(str(downloaded), destination)
        return DownloadResult(target_url, True, "Downloaded", destination)

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

        targets = extract_targets(self.text_box.get("1.0", tk.END))
        if not targets:
            messagebox.showwarning("No targets", "No DOIs or URLs were found in the provided text.")
            return

        destination = Path(self.path_var.get()).expanduser()
        destination.mkdir(parents=True, exist_ok=True)

        self.status_var.set("Starting downloads...")
        self.download_button.config(state=tk.DISABLED)
        self.download_thread = threading.Thread(
            target=self._run_downloads, args=(targets, destination), daemon=True
        )
        self.download_thread.start()

    def _run_downloads(self, targets: List[str], destination: Path) -> None:
        temp_dir = Path(tempfile.mkdtemp(prefix="fetch_pdfs_"))
        results: List[DownloadResult] = []
        try:
            self.downloader = PDFDownloader(temp_dir)
            for index, target in enumerate(targets, start=1):
                output_name = f"reference_{index:03d}"
                self._update_status(f"Downloading {index}/{len(targets)}: {target}")
                result = self.downloader.download(target, output_name, destination)
                results.append(result)
        except Exception as exc:  # pragma: no cover - GUI feedback only
            results.append(DownloadResult("Initialization", False, str(exc)))
        finally:
            if self.downloader:
                self.downloader.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

        success = sum(1 for r in results if r.success)
        failures = [r for r in results if not r.success]

        summary_lines = [f"Downloaded {success} of {len(targets)} references."]
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
