"""Utility for downloading PDFs referenced in a bibliography using Selenium.

The script presents a small GUI where users can paste a bibliography and
optionally change the download directory or browser (Edge or Chrome).  Each
entry is parsed for DOIs or direct links, and Selenium is used to automate a
browser session that navigates to the DOI landing page and attempts to locate a
PDF download link.

The script assumes that the user has access rights to the requested material
through their browser session.  Browser drivers are managed automatically via
``webdriver-manager``.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

import requests


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
URL_PATTERN = re.compile(r"https?://[^\s<>]+")


class DownloadError(Exception):
    """Custom error raised when a PDF download fails."""


@dataclass
class ReferenceLink:
    """Representation of an extracted DOI/URL along with the source line."""

    source: str
    url: str


def parse_bibliography(text: str) -> List[ReferenceLink]:
    """Extract DOI and HTTP links from bibliography text.

    Args:
        text: Raw bibliography text supplied by the user.

    Returns:
        A list of ``ReferenceLink`` entries with normalized URLs.
    """

    links: List[ReferenceLink] = []
    for line in text.splitlines():
        if not line.strip():
            continue

        urls = list(URL_PATTERN.findall(line))
        if urls:
            links.extend(ReferenceLink(source=line.strip(), url=url) for url in urls)
            continue

        dois = list(DOI_PATTERN.findall(line))
        for doi in dois:
            normalized = f"https://doi.org/{doi.strip()}"
            links.append(ReferenceLink(source=line.strip(), url=normalized))

    # Deduplicate while preserving order.
    seen = set()
    unique_links = []
    for link in links:
        if link.url not in seen:
            seen.add(link.url)
            unique_links.append(link)
    return unique_links


def _resolve_manual_driver(browser: str, errors: List[str]) -> Optional[Path]:
    """Return a manually supplied driver path via env vars or PATH."""

    env_map = {
        "chrome": "CHROME_DRIVER_PATH",
        "edge": "EDGE_DRIVER_PATH",
    }
    env_var = env_map.get(browser)
    if env_var:
        configured = os.environ.get(env_var)
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.exists():
                return candidate
            errors.append(f"{env_var} is set but {candidate} does not exist.")

    command_names = ["chromedriver"] if browser == "chrome" else ["msedgedriver", "msedgedriver.exe"]
    for name in command_names:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved)
    return None


def build_driver(browser: str, download_dir: Path) -> webdriver.Remote:
    """Create a Selenium WebDriver instance for the chosen browser."""

    download_dir.mkdir(parents=True, exist_ok=True)
    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }

    if browser == "chrome":
        options = ChromeOptions()
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--start-maximized")

        errors: List[str] = []

        manual_driver = _resolve_manual_driver("chrome", errors)
        service: Optional[ChromeService]
        if manual_driver:
            service = ChromeService(executable_path=str(manual_driver))
        else:
            try:
                service = ChromeService(ChromeDriverManager().install())
            except (requests.exceptions.RequestException, ValueError) as exc:
                errors.append(
                    "Automatic Chrome driver download failed. "
                    "Set CHROME_DRIVER_PATH to an existing driver executable or add it to PATH."
                )
                errors.append(str(exc))
                service = None

        if not service:
            if not errors:
                errors.append(
                    "Chrome driver executable not found. Set CHROME_DRIVER_PATH or place chromedriver on PATH."
                )
            raise WebDriverException("\n".join(errors))

        driver = webdriver.Chrome(service=service, options=options)
    else:
        options = EdgeOptions()
        options.use_chromium = True
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--start-maximized")

        errors: List[str] = []

        manual_driver = _resolve_manual_driver("edge", errors)
        service: Optional[EdgeService]
        if manual_driver:
            service = EdgeService(executable_path=str(manual_driver))
        else:
            try:
                service = EdgeService(EdgeChromiumDriverManager().install())
            except (requests.exceptions.RequestException, ValueError) as exc:
                errors.append(
                    "Automatic Edge driver download failed. "
                    "Set EDGE_DRIVER_PATH to an existing driver executable or add it to PATH."
                )
                errors.append(str(exc))
                service = None

        if not service:
            if not errors:
                errors.append(
                    "Edge driver executable not found. Set EDGE_DRIVER_PATH or place msedgedriver on PATH."
                )
            raise WebDriverException("\n".join(errors))

        driver = webdriver.Edge(service=service, options=options)

    driver.set_page_load_timeout(120)
    return driver


def _find_pdf_element(driver: webdriver.Remote) -> Optional[str]:
    """Attempt to find a PDF link or button and return its href."""

    # Direct anchors containing PDF in text or href.
    anchor_queries = [
        "//a[contains(translate(text(),'PDF','pdf'),'pdf')]",
        "//a[contains(translate(@title,'PDF','pdf'),'pdf')]",
        "//a[contains(translate(@href,'PDF','pdf'),'pdf')]",
    ]

    for query in anchor_queries:
        elements = driver.find_elements(By.XPATH, query)
        for element in elements:
            href = element.get_attribute("href")
            if href and href.lower().endswith(".pdf"):
                return href
            if href and "pdf" in href.lower():
                return href

    # Buttons that may trigger downloads by clicking.
    button_queries = [
        "//button[contains(translate(text(),'PDF','pdf'),'pdf')]",
        "//a[contains(@role,'button') and contains(translate(text(),'PDF','pdf'),'pdf')]",
    ]
    for query in button_queries:
        elements = driver.find_elements(By.XPATH, query)
        for element in elements:
            href = element.get_attribute("href")
            if href:
                return href
            try:
                element.click()
                time.sleep(2)
            except WebDriverException:
                continue

            # After clicking, search again for new anchors.
            refreshed = driver.find_elements(By.XPATH, anchor_queries[2])
            for candidate in refreshed:
                href = candidate.get_attribute("href")
                if href and href.lower().endswith(".pdf"):
                    return href
    return None


def wait_for_download(download_dir: Path, before: Sequence[Path], timeout: int = 180) -> Optional[Path]:
    """Wait until a new PDF appears in the download directory."""

    baseline = {p.resolve() for p in before}
    end = time.time() + timeout
    while time.time() < end:
        for candidate in download_dir.glob("*.pdf"):
            resolved = candidate.resolve()
            if resolved not in baseline:
                # Ensure the browser finished writing the file.
                if not candidate.with_suffix(candidate.suffix + ".crdownload").exists():
                    return resolved
        time.sleep(1)
    return None


def download_pdf(driver: webdriver.Remote, link: ReferenceLink, download_dir: Path) -> Path:
    """Navigate to the DOI/URL and attempt to download the PDF."""

    driver.get(link.url)
    time.sleep(5)  # Allow time for redirects/JS frameworks.

    pdf_href = _find_pdf_element(driver)
    if not pdf_href:
        raise DownloadError(f"Could not locate a PDF link for {link.url}")

    before_files = list(download_dir.glob("*"))

    try:
        driver.get(pdf_href)
    except WebDriverException as exc:
        raise DownloadError(f"Failed to open PDF link for {link.url}: {exc}") from exc

    downloaded = wait_for_download(download_dir, before_files)
    if not downloaded:
        raise DownloadError(f"Timed out waiting for PDF download for {link.url}")

    return downloaded


class DownloadApp:
    """Graphical interface for orchestrating PDF downloads."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Bibliography PDF Fetcher")
        self.root.geometry("800x600")

        self.browser_var = tk.StringVar(value="edge")
        self.folder_var = tk.StringVar(value=str(default_download_folder()))

        self._build_widgets()

        self.log_messages: List[str] = []
        self.driver: Optional[webdriver.Remote] = None
        self.active_thread: Optional[threading.Thread] = None

    def _build_widgets(self) -> None:
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text="Paste bibliography entries below:").pack(anchor=tk.W)

        self.text = tk.Text(main_frame, wrap=tk.WORD)
        self.text.pack(fill=tk.BOTH, expand=True, pady=5)

        controls = ttk.Frame(main_frame)
        controls.pack(fill=tk.X, pady=5)

        ttk.Label(controls, text="Download folder:").pack(side=tk.LEFT)
        folder_entry = ttk.Entry(controls, textvariable=self.folder_var, width=60)
        folder_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(controls, text="Change…", command=self.choose_folder).pack(side=tk.LEFT)

        browser_frame = ttk.Frame(main_frame)
        browser_frame.pack(fill=tk.X, pady=5)
        ttk.Label(browser_frame, text="Browser:").pack(side=tk.LEFT)
        browser_menu = ttk.OptionMenu(
            browser_frame,
            self.browser_var,
            self.browser_var.get(),
            "edge",
            "chrome",
        )
        browser_menu.pack(side=tk.LEFT, padx=5)

        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=10)
        ttk.Button(button_frame, text="Download PDFs", command=self.start_download).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="Quit", command=self.on_quit).pack(side=tk.RIGHT)

        self.log_widget = tk.Text(main_frame, height=10, state=tk.DISABLED, wrap=tk.WORD)
        self.log_widget.pack(fill=tk.BOTH, expand=False)

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.folder_var.get())
        if selected:
            self.folder_var.set(selected)

    def start_download(self) -> None:
        if self.active_thread and self.active_thread.is_alive():
            messagebox.showinfo("Download in progress", "Please wait for the current download to finish.")
            return

        bibliography = self.text.get("1.0", tk.END)
        links = parse_bibliography(bibliography)
        if not links:
            messagebox.showerror("No links found", "Could not extract any DOIs or URLs from the bibliography.")
            return

        folder = Path(self.folder_var.get()).expanduser()
        browser_choice = self.browser_var.get().lower()
        if browser_choice not in {"edge", "chrome"}:
            browser_choice = "edge"

        self.active_thread = threading.Thread(
            target=self._download_worker,
            args=(links, folder, browser_choice),
            daemon=True,
        )
        self.active_thread.start()

    def _download_worker(self, links: Sequence[ReferenceLink], folder: Path, browser: str) -> None:
        self._log(f"Starting download of {len(links)} item(s) using {browser.title()}…")
        try:
            self.driver = build_driver(browser, folder)
        except WebDriverException as exc:
            self._log(f"Failed to initialise browser: {exc}")
            messagebox.showerror("Browser error", f"Failed to start browser: {exc}")
            return

        success_count = 0
        try:
            for link in links:
                self._log(f"Processing: {link.url}")
                try:
                    path = download_pdf(self.driver, link, folder)
                except (DownloadError, TimeoutException, NoSuchElementException, WebDriverException) as exc:
                    self._log(f"  ❌ {exc}")
                    continue
                else:
                    success_count += 1
                    self._log(f"  ✅ Saved to {path.name}")
        finally:
            if self.driver:
                self.driver.quit()
                self.driver = None

        self._log(f"Completed with {success_count}/{len(links)} successful downloads.")

    def _log(self, message: str) -> None:
        timestamp = time.strftime("[%H:%M:%S]")
        entry = f"{timestamp} {message}\n"
        self.log_messages.append(entry)
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, entry)
        self.log_widget.configure(state=tk.DISABLED)
        self.log_widget.see(tk.END)

    def on_quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def default_download_folder() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.exists() else Path.home()


def main() -> None:
    app = DownloadApp()
    app.run()


if __name__ == "__main__":
    main()
