"""GUI tool to download bibliography PDFs by visiting DOI pages in a browser.

Run with ``python main.py``. A Tkinter window appears where the user can paste a
bibliography, choose the download directory, and pick a browser (Edge or
Chrome). When the user clicks *Download PDFs* the script spins up Selenium,
opens each DOI in the selected browser, clicks on PDF links/buttons, and waits
for the file to finish downloading in the chosen folder.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Set

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


class DownloadError(Exception):
    """Raised when a PDF could not be downloaded for a DOI."""


def extract_dois(bibliography: str) -> List[str]:
    """Return a list of unique DOIs found in the bibliography text."""

    found: Set[str] = set()
    dois: List[str] = []
    for match in DOI_PATTERN.finditer(bibliography):
        doi = match.group().strip().rstrip('.')
        if doi.lower() not in found:
            found.add(doi.lower())
            dois.append(doi)
    return dois


def normalise_url(identifier: str) -> str:
    """Ensure the DOI identifier is a usable URL."""

    identifier = identifier.strip()
    if identifier.startswith("http://") or identifier.startswith("https://"):
        return identifier
    return f"https://doi.org/{identifier}"


def configure_edge(download_dir: Path) -> webdriver.Edge:
    """Create an Edge WebDriver configured to auto-download PDFs."""

    options = EdgeOptions()
    options.use_chromium = True
    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--disable-infobars")
    options.add_argument("--start-maximized")
    service = EdgeService(executable_path=EdgeChromiumDriverManager().install())
    return webdriver.Edge(service=service, options=options)


def configure_chrome(download_dir: Path) -> webdriver.Chrome:
    """Create a Chrome WebDriver configured to auto-download PDFs."""

    options = ChromeOptions()
    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--disable-infobars")
    options.add_argument("--start-maximized")
    service = ChromeService(executable_path=ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def find_pdf_link(driver: webdriver.Remote) -> Optional[str]:
    """Search the current page for an obvious PDF download link."""

    selectors = [
        "//a[contains(translate(text(),'PDF','pdf'),'pdf')]",
        "//a[contains(translate(@title,'PDF','pdf'),'pdf')]",
        "//a[contains(translate(@aria-label,'PDF','pdf'),'pdf')]",
        "//a[contains(translate(@href,'PDF','pdf'),'.pdf')]",
        "//button[contains(translate(text(),'PDF','pdf'),'pdf')]",
    ]
    for selector in selectors:
        try:
            element = driver.find_element(By.XPATH, selector)
        except NoSuchElementException:
            continue
        href = element.get_attribute("href")
        if href:
            return href
        try:
            element.click()
        except Exception:
            pass
        time.sleep(1)
        current = driver.current_url
        if current.lower().endswith(".pdf"):
            return current
    current = driver.current_url
    if current.lower().endswith(".pdf"):
        return current
    return None


def wait_for_download(download_dir: Path, known_files: Set[Path], timeout: float = 120.0) -> Path:
    """Wait for a new file to appear in download_dir that is not in known_files."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        entries = set(download_dir.iterdir())
        new_files = [f for f in entries if f not in known_files and f.is_file() and not f.name.startswith(".~")]
        partials = [f for f in new_files if f.suffix in {".crdownload", ".part"}]
        if partials:
            time.sleep(0.5)
            continue
        if new_files:
            return sorted(new_files, key=lambda p: p.stat().st_mtime)[-1]
        time.sleep(0.5)
    raise TimeoutException("Timed out waiting for PDF download to finish.")


def download_pdf(driver: webdriver.Remote, doi: str, download_dir: Path) -> Path:
    """Download the PDF for the provided DOI and return the file path."""

    url = normalise_url(doi)
    driver.get(url)
    WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState") == "complete")
    pdf_url = find_pdf_link(driver)
    if not pdf_url:
        raise DownloadError("Could not locate a PDF link on the landing page.")

    # Track existing files to identify the new download.
    known_files = set(download_dir.glob("*"))

    if driver.current_url.lower().endswith(".pdf") and driver.current_url == pdf_url:
        # The DOI redirected directly to a PDF – just wait for the download.
        pass
    else:
        driver.get(pdf_url)

    downloaded_file = wait_for_download(download_dir, known_files)
    return downloaded_file


class BibliographyDownloader(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Bibliography PDF Downloader")
        self.geometry("700x600")

        self.browser_var = tk.StringVar(value="Edge")
        self.directory_var = tk.StringVar(value=str(self._default_download_dir()))

        self._build_widgets()

    @staticmethod
    def _default_download_dir() -> Path:
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            return desktop
        return Path.home()

    def _build_widgets(self) -> None:
        padding = {"padx": 10, "pady": 5}

        ttk.Label(self, text="Paste bibliography entries (with DOIs) below:").grid(row=0, column=0, sticky="w", **padding)

        self.text = tk.Text(self, wrap="word", height=15)
        self.text.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10)

        scrollbar = ttk.Scrollbar(self, command=self.text.yview)
        scrollbar.grid(row=1, column=3, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)

        ttk.Label(self, text="Download folder:").grid(row=2, column=0, sticky="w", **padding)
        self.dir_entry = ttk.Entry(self, textvariable=self.directory_var, width=60)
        self.dir_entry.grid(row=3, column=0, sticky="we", padx=(10, 0))
        ttk.Button(self, text="Browse", command=self._choose_directory).grid(row=3, column=1, sticky="w", **padding)

        ttk.Label(self, text="Browser:").grid(row=2, column=2, sticky="e", **padding)
        browser_box = ttk.Combobox(self, textvariable=self.browser_var, values=["Edge", "Chrome"], state="readonly")
        browser_box.grid(row=3, column=2, sticky="e", padx=(0, 10))

        ttk.Button(self, text="Download PDFs", command=self._start_download).grid(row=4, column=0, sticky="w", **padding)

        ttk.Label(self, text="Activity log:").grid(row=5, column=0, sticky="w", **padding)

        self.log = tk.Text(self, wrap="word", height=15, state="disabled", background="#f7f7f7")
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew", padx=10, pady=(0, 10))

        log_scrollbar = ttk.Scrollbar(self, command=self.log.yview)
        log_scrollbar.grid(row=6, column=3, sticky="ns", pady=(0, 10))
        self.log.configure(yscrollcommand=log_scrollbar.set)

        # Grid configuration for resizing
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(6, weight=1)
        self.grid_columnconfigure(0, weight=1)

    def _choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.directory_var.get() or str(self._default_download_dir()))
        if selected:
            self.directory_var.set(selected)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start_download(self) -> None:
        bibliography = self.text.get("1.0", "end").strip()
        if not bibliography:
            messagebox.showwarning("No entries", "Please paste or type your bibliography entries before downloading.")
            return

        download_dir = Path(self.directory_var.get()).expanduser()
        download_dir.mkdir(parents=True, exist_ok=True)

        dois = extract_dois(bibliography)
        if not dois:
            messagebox.showwarning("No DOIs found", "No DOIs were detected in the provided text.")
            return

        self._append_log(f"Found {len(dois)} DOI(s). Starting downloads using {self.browser_var.get()}...")
        threading.Thread(
            target=self._download_worker,
            args=(dois, download_dir, self.browser_var.get()),
            daemon=True,
        ).start()

    def _download_worker(self, dois: Iterable[str], download_dir: Path, browser_choice: str) -> None:
        try:
            driver = self._build_driver(browser_choice, download_dir)
        except Exception as exc:  # pragma: no cover - GUI convenience
            self._append_log(f"Failed to start browser: {exc}")
            return

        try:
            for doi in dois:
                self._append_log(f"Processing DOI: {doi}")
                try:
                    pdf_path = download_pdf(driver, doi, download_dir)
                except DownloadError as err:
                    self._append_log(f"  ✗ {doi}: {err}")
                except TimeoutException:
                    self._append_log(f"  ✗ {doi}: Timed out waiting for the PDF download.")
                except Exception as exc:
                    self._append_log(f"  ✗ {doi}: {exc}")
                else:
                    self._append_log(f"  ✓ Saved to {pdf_path.name}")
        finally:
            driver.quit()
            self._append_log("All tasks complete. You may close the browser if it is still open.")

    @staticmethod
    def _build_driver(browser_choice: str, download_dir: Path) -> webdriver.Remote:
        if browser_choice.lower() == "chrome":
            return configure_chrome(download_dir)
        return configure_edge(download_dir)


def main() -> None:
    app = BibliographyDownloader()
    app.mainloop()


if __name__ == "__main__":
    main()

