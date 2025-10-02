"""Automation script to download PDF files referenced in a bibliography.

This script expects selenium to be available and that Chrome/Chromium is
installed on the machine running it.  It parses a plaintext bibliography,
opens each DOI in a real browser session, clicks the PDF download button, and
moves the resulting files into a folder on the user's Desktop.

Because the actual PDF retrieval relies on the current user having access to
paid content, authentication should be performed ahead of time in the browser
profile used by Selenium (for example by logging in to the publisher's site in
Chrome beforehand).
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Set

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s;]+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
PDF_EXTENSIONS = (".pdf",)
DOWNLOAD_WAIT_SECONDS = 900
POLL_INTERVAL = 0.5


@dataclasses.dataclass(frozen=True)
class Reference:
    """Representation of a single bibliography entry."""

    label: str
    raw_text: str
    url: str

    @property
    def safe_slug(self) -> str:
        """Return a filesystem friendly slug based on the reference label/text."""

        base = f"{self.label} {self.raw_text[:80]}"
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
        return sanitized[:120] if sanitized else "reference"


class DownloadTimeoutError(RuntimeError):
    """Raised when a download fails to complete in the allotted time."""


def parse_references(lines: Sequence[str]) -> List[Reference]:
    """Extract unique references and their DOI/URL destinations.

    Parameters
    ----------
    lines:
        Lines from a bibliography text file.  Empty lines are ignored.

    Returns
    -------
    List[Reference]
        Unique references keyed by their label (if present) or a running index.
    """

    seen_urls: Set[str] = set()
    references: List[Reference] = []

    for idx, line in enumerate(line for line in (ln.strip() for ln in lines) if line):
        label_match = re.match(r"^\(([^)]+)\)", line)
        label = label_match.group(1) if label_match else f"ref_{idx + 1}"

        url_match = URL_PATTERN.search(line)
        if url_match:
            url = url_match.group(0)
        else:
            doi_match = DOI_PATTERN.search(line)
            if not doi_match:
                print(f"[warn] No DOI or URL found for entry: {line}", file=sys.stderr)
                continue
            url = f"https://doi.org/{doi_match.group(0)}"

        url = url.rstrip(". ,")
        if url in seen_urls:
            continue

        seen_urls.add(url)
        references.append(Reference(label=label, raw_text=line, url=url))

    return references


def build_chrome(download_dir: Path, headless: bool = False, profile: Optional[Path] = None) -> webdriver.Chrome:
    """Create and configure a Chrome WebDriver instance."""

    download_dir.mkdir(parents=True, exist_ok=True)

    options = Options()
    prefs = {
        "download.default_directory": str(download_dir.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    if headless:
        # Chrome's "new" headless mode supports downloads.
        options.add_argument("--headless=new")

    if profile:
        options.add_argument(f"--user-data-dir={profile}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(120)
    return driver


def list_downloaded_files(download_dir: Path) -> Set[Path]:
    return {path for path in download_dir.iterdir() if path.is_file()}


def wait_for_new_file(initial_files: Set[Path], download_dir: Path, *, timeout: float) -> Path:
    """Block until a new file (without Chrome temporary extensions) appears."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        current_files = list_downloaded_files(download_dir)
        new_files = [path for path in current_files - initial_files if not path.suffix.endswith("crdownload")]
        if new_files:
            stable_files = [path for path in new_files if not path.name.endswith(".crdownload")]
            if stable_files:
                latest = max(stable_files, key=lambda p: p.stat().st_mtime)
                # Ensure Chrome finished writing the file.
                size = -1
                while time.time() < deadline:
                    new_size = latest.stat().st_size
                    if new_size == size:
                        return latest
                    size = new_size
                    time.sleep(POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)

    raise DownloadTimeoutError("Timed out waiting for PDF download to complete.")


def find_pdf_element(driver: webdriver.Chrome) -> Optional[object]:
    """Return a WebElement likely triggering a PDF download."""

    candidates = []
    for locator in (
        (By.PARTIAL_LINK_TEXT, "PDF"),
        (By.XPATH, "//a[contains(@href, '.pdf')]") ,
        (By.XPATH, "//button[contains(translate(., 'pdf', 'PDF'), 'PDF')]")
    ):
        candidates.extend(driver.find_elements(*locator))

    for element in candidates:
        if element.is_displayed() and element.is_enabled():
            return element

    return None


def trigger_pdf_download(driver: webdriver.Chrome, reference: Reference) -> None:
    """Navigate to a reference URL and attempt to download its PDF."""

    driver.get(reference.url)
    wait = WebDriverWait(driver, 60)

    try:
        wait.until(lambda drv: drv.execute_script("return document.readyState") == "complete")
    except TimeoutException:
        print(f"[warn] Timed out waiting for page to load: {reference.url}", file=sys.stderr)

    pdf_element = find_pdf_element(driver)
    if pdf_element:
        driver.execute_script("arguments[0].click();", pdf_element)
        return

    current_url = driver.current_url
    if current_url.lower().endswith(PDF_EXTENSIONS):
        # Some DOIs redirect directly to a PDF. Trigger a second GET to force download.
        driver.get(current_url)
        return

    raise RuntimeError(
        "Could not locate a PDF download control. Manual intervention may be required "
        f"for {reference.url}."
    )


def move_and_rename(file_path: Path, destination_dir: Path, reference: Reference) -> Path:
    """Move the downloaded PDF into the final destination with a readable name."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{reference.safe_slug}.pdf"

    # Avoid overwriting files; append an incrementing suffix if necessary.
    counter = 1
    while target.exists():
        target = destination_dir / f"{reference.safe_slug}_{counter}.pdf"
        counter += 1

    shutil.move(str(file_path), target)
    return target


def process_references(
    references: Sequence[Reference],
    *,
    temp_download_dir: Path,
    final_destination: Path,
    headless: bool,
    profile: Optional[Path] = None,
) -> List[Path]:
    """Download PDFs for each reference, returning the final file paths."""

    downloaded: List[Path] = []
    driver = build_chrome(temp_download_dir, headless=headless, profile=profile)

    try:
        for reference in references:
            print(f"[info] Processing {reference.label}: {reference.url}")
            existing_files = list_downloaded_files(temp_download_dir)
            trigger_pdf_download(driver, reference)
            try:
                new_file = wait_for_new_file(existing_files, temp_download_dir, timeout=DOWNLOAD_WAIT_SECONDS)
            except DownloadTimeoutError as exc:
                print(f"[error] {exc}", file=sys.stderr)
                continue

            final_path = move_and_rename(new_file, final_destination, reference)
            downloaded.append(final_path)
            print(f"[info] Saved to {final_path}")
    finally:
        driver.quit()

    return downloaded


def read_bibliography(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"Bibliography file not found: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download bibliography PDFs via Selenium.")
    parser.add_argument(
        "bibliography",
        type=Path,
        help="Path to a text file containing bibliography entries.",
    )
    parser.add_argument(
        "--folder-name",
        default="Fetched_PDFs",
        help="Name of the folder to create on the Desktop for the final PDFs.",
    )
    parser.add_argument(
        "--temp-download-dir",
        type=Path,
        default=Path.home() / "Downloads" / "selenium_pdf_temp",
        help="Temporary folder for Selenium downloads (defaults to ~/Downloads/selenium_pdf_temp).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome in headless mode.",
    )
    parser.add_argument(
        "--chrome-profile",
        type=Path,
        default=None,
        help="Optional path to a Chrome user data directory with existing logins.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    lines = read_bibliography(args.bibliography)
    references = parse_references(lines)

    if not references:
        print("No references containing DOIs or URLs were found.", file=sys.stderr)
        return 1

    desktop = Path.home() / "Desktop"
    destination_dir = desktop / args.folder_name

    downloaded = process_references(
        references,
        temp_download_dir=args.temp_download_dir,
        final_destination=destination_dir,
        headless=args.headless,
        profile=args.chrome_profile,
    )

    if not downloaded:
        print("No files were downloaded.", file=sys.stderr)
        return 2

    print("\nCompleted downloads:")
    for path in downloaded:
        print(f" - {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
