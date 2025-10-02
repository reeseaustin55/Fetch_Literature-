"""Bibliography PDF downloader GUI application.

This script provides a Tkinter-based interface that accepts a bibliography
string, extracts DOI/URL links, and uses Selenium to download the associated
PDF files to a user-selected folder. Edge (Chromium) is used by default with an
option to switch to Chrome.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set

import requests
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from selenium import webdriver
from selenium.common.exceptions import (NoSuchElementException,
                                        TimeoutException,
                                        WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions

from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager


URL_PATTERN = re.compile(r"(https?://[^\s)]+)")
PDF_KEYWORDS = [
    "PDF",
    "Full Text PDF",
    "Download PDF",
    "View PDF",
    "Article PDF",
    "Get PDF",
]
DOWNLOAD_POLL_INTERVAL = 1.0  # seconds


def extract_urls(bibliography: str) -> List[str]:
    """Extract unique URLs/DOIs from the bibliography string."""
    urls = []
    seen: Set[str] = set()
    for match in URL_PATTERN.finditer(bibliography):
        url = match.group(1).strip().rstrip('.')
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


@dataclass
class DownloadResult:
    url: str
    success: bool
    message: str


class PDFDownloader:
    """Manage Selenium interactions to download PDFs."""

    def __init__(self, download_dir: Path, browser: str, log_callback):
        self.download_dir = download_dir
        self.browser = browser.lower()
        self.log = log_callback
        self.driver: Optional[webdriver.Remote] = None

    def __enter__(self) -> "PDFDownloader":
        self.start_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop_driver()

    # Driver management -------------------------------------------------
    def start_driver(self) -> None:
        if self.driver is not None:
            return

        prefs = {
            "download.default_directory": str(self.download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        }

        if self.browser == "chrome":
            options = ChromeOptions()
            options.add_experimental_option("prefs", prefs)
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            service = ChromeService(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
        else:  # default to Edge
            options = EdgeOptions()
            options.use_chromium = True
            options.add_experimental_option("prefs", prefs)
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            service = EdgeService(EdgeChromiumDriverManager().install())
            self.driver = webdriver.Edge(service=service, options=options)

        self.driver.set_page_load_timeout(60)
        self.log(f"Started {self.browser.title()} browser session.")

    def stop_driver(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
                self.log("Closed browser session.")
            except WebDriverException as exc:
                self.log(f"Warning: failed to close browser cleanly: {exc}")
        self.driver = None

    # Download helpers --------------------------------------------------
    def download_all(self, urls: Iterable[str]) -> List[DownloadResult]:
        results: List[DownloadResult] = []
        if not urls:
            self.log("No URLs or DOIs detected in the provided bibliography.")
            return results

        self.start_driver()
        assert self.driver is not None

        for url in urls:
            self.log(f"Processing: {url}")
            try:
                result = self._download_single(url)
            except Exception as exc:  # pylint: disable=broad-except
                result = DownloadResult(url=url, success=False, message=str(exc))
            results.append(result)
            if result.success:
                self.log(f"✓ Downloaded: {url}")
            else:
                self.log(f"✗ Failed for {url}: {result.message}")
        return results

    def _download_single(self, url: str) -> DownloadResult:
        assert self.driver is not None
        driver = self.driver
        snapshot = self._snapshot_downloads()
        driver.get(url)
        self._wait_for_ready_state()

        if self._detect_pdf_viewer(url):
            return DownloadResult(url, True, "PDF URL fetched directly.")

        if self._try_click_pdf_link(snapshot):
            return DownloadResult(url, True, "PDF downloaded via link.")

        # As a fallback, attempt to download the current URL if it ends with PDF
        if self._download_current_url_as_pdf():
            return DownloadResult(url, True, "Downloaded PDF from current tab.")

        return DownloadResult(url, False, "Could not locate a PDF download link.")

    def _wait_for_ready_state(self, timeout: int = 30) -> None:
        assert self.driver is not None
        driver = self.driver
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            self.log("Page did not reach ready state, continuing anyway.")

    def _snapshot_downloads(self) -> Set[Path]:
        if not self.download_dir.exists():
            self.download_dir.mkdir(parents=True, exist_ok=True)
        return set(self.download_dir.glob("*"))

    def _detect_pdf_viewer(self, initial_url: str) -> bool:
        """Detect if the current page is already rendering a PDF and save it."""
        assert self.driver is not None
        current_url = self.driver.current_url
        if current_url.lower().endswith(".pdf"):
            return self._download_via_requests(current_url)
        if current_url != initial_url and current_url.lower().endswith(".pdf"):
            return self._download_via_requests(current_url)
        return False

    def _try_click_pdf_link(self, snapshot: Set[Path]) -> bool:
        assert self.driver is not None
        driver = self.driver
        wait = WebDriverWait(driver, 20)
        candidates = self._find_pdf_candidates()
        for element in candidates:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", element
                )
                wait.until(EC.element_to_be_clickable(element))
                before = snapshot | set(self.download_dir.glob("*"))
                element.click()
                if self._wait_for_download(before):
                    return True
            except (TimeoutException, WebDriverException) as exc:
                self.log(f"Click attempt failed: {exc}")
        return False

    def _find_pdf_candidates(self):
        assert self.driver is not None
        driver = self.driver
        xpath_conditions = []
        for keyword in PDF_KEYWORDS:
            keyword_upper = keyword.upper()
            xpath_conditions.append(
                (
                    f"//*[self::a or self::button or self::span or self::div]"
                    f"[contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz',"
                    f" 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), '{keyword_upper}')]")
            )
        xpath = " | ".join(xpath_conditions)
        if not xpath:
            return []
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            return [elem for elem in elements if elem.is_displayed()]
        except NoSuchElementException:
            return []

    def _wait_for_download(self, before: Set[Path], timeout: int = 120) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_files = set(self.download_dir.glob("*"))
            new_files = current_files - before
            completed = [
                f for f in new_files if not f.name.endswith((".crdownload", ".tmp", ".part"))
            ]
            if completed:
                return True
            time.sleep(DOWNLOAD_POLL_INTERVAL)
        return False

    def _download_current_url_as_pdf(self) -> bool:
        assert self.driver is not None
        current_url = self.driver.current_url
        if current_url.lower().endswith(".pdf"):
            return self._download_via_requests(current_url)
        return False

    def _download_via_requests(self, pdf_url: str) -> bool:
        """Download the PDF using the session cookies from Selenium."""
        assert self.driver is not None
        session = requests.Session()
        for cookie in self.driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"])
        try:
            response = session.get(pdf_url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            self.log(f"Direct PDF download failed: {exc}")
            return False

        filename = self._derive_filename(pdf_url, response.headers.get("content-disposition"))
        filepath = self.download_dir / filename
        try:
            with open(filepath, "wb") as file:
                file.write(response.content)
            return True
        except OSError as exc:
            self.log(f"Failed to save PDF: {exc}")
            return False

    def _derive_filename(self, url: str, content_disposition: Optional[str]) -> str:
        if content_disposition:
            match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition)
            if match:
                return match.group(1)
            match = re.search(r'filename="?([^";]+)"?', content_disposition)
            if match:
                return match.group(1)
        name = url.rstrip("/").split("/")[-1]
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        return name


class BibliographyDownloaderApp:
    """Tkinter GUI to manage bibliography PDF downloads."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Bibliography PDF Downloader")
        self.root.geometry("720x600")

        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self._build_widgets()
        self.root.after(100, self._process_log_queue)

    # GUI setup ---------------------------------------------------------
    def _build_widgets(self) -> None:
        main_frame = tk.Frame(self.root, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(main_frame, text="Paste bibliography entries:").pack(anchor=tk.W)

        self.bibliography_text = ScrolledText(main_frame, height=15)
        self.bibliography_text.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        folder_frame = tk.Frame(main_frame)
        folder_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(folder_frame, text="Download folder:").pack(anchor=tk.W)
        self.folder_var = tk.StringVar(value=str(self._default_download_folder()))
        folder_entry = tk.Entry(folder_frame, textvariable=self.folder_var)
        folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(folder_frame, text="Browse", command=self._select_folder).pack(
            side=tk.LEFT, padx=(5, 0)
        )

        browser_frame = tk.Frame(main_frame)
        browser_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(browser_frame, text="Browser:").pack(anchor=tk.W)
        self.browser_var = tk.StringVar(value="edge")
        tk.Radiobutton(
            browser_frame, text="Microsoft Edge (default)", variable=self.browser_var, value="edge"
        ).pack(anchor=tk.W)
        tk.Radiobutton(
            browser_frame, text="Google Chrome", variable=self.browser_var, value="chrome"
        ).pack(anchor=tk.W)

        self.start_button = tk.Button(
            main_frame, text="Download PDFs", command=self.start_download
        )
        self.start_button.pack(pady=(0, 10))

        tk.Label(main_frame, text="Activity log:").pack(anchor=tk.W)
        self.log_text = ScrolledText(main_frame, height=10, state="disabled")
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _default_download_folder(self) -> Path:
        desktop = Path.home() / "Desktop"
        return desktop / "Bibliography_PDFs"

    def _select_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.folder_var.get())
        if selected:
            self.folder_var.set(selected)

    # Logging -----------------------------------------------------------
    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _process_log_queue(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert(tk.END, message + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._process_log_queue)

    # Download orchestration -------------------------------------------
    def start_download(self) -> None:
        bibliography = self.bibliography_text.get("1.0", tk.END).strip()
        if not bibliography:
            messagebox.showwarning("Missing bibliography", "Please paste your bibliography text.")
            return

        folder = Path(self.folder_var.get()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Folder error", f"Unable to access folder: {exc}")
            return

        urls = extract_urls(bibliography)
        if not urls:
            messagebox.showinfo("No URLs found", "No DOI or URL links were detected in the text.")
            return

        self.start_button.configure(state=tk.DISABLED)
        self.log("Starting download task...")
        thread = threading.Thread(
            target=self._run_downloader, args=(urls, folder, self.browser_var.get()), daemon=True
        )
        thread.start()

    def _run_downloader(self, urls: List[str], folder: Path, browser: str) -> None:
        try:
            with PDFDownloader(folder, browser, self.log) as downloader:
                results = downloader.download_all(urls)
            successes = sum(1 for result in results if result.success)
            failures = len(results) - successes
            self.log(f"Completed downloads. Successes: {successes}, Failures: {failures}")
        finally:
            self.start_button.configure(state=tk.NORMAL)

    # App loop ---------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = BibliographyDownloaderApp()
    app.run()


if __name__ == "__main__":
    main()
