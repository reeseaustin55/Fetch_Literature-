"""GUI application to download PDFs for a pasted bibliography.

The application presents a window containing a large text box. Users can
paste the bibliography of a paper, click the *Download PDFs* button, and the
script will attempt to resolve each citation to a PDF. The downloaded files
are saved to a folder on the user's desktop.

The fetcher uses two strategies:
1. If a DOI is present in the citation, the script resolves it via doi.org and
   attempts to download the PDF directly.
2. If no DOI is detected, the Crossref *query.bibliographic* endpoint is used
   to look for a matching record that advertises a PDF link.

For the Crossref API the script is able to provide a contact email via the
``CROSSREF_CONTACT_EMAIL`` environment variable. Although optional, supplying a
real email increases the chance of successful requests when making many calls.

Because network operations can take time, downloads are executed in a worker
thread while log updates are posted back to the Tkinter event loop.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from queue import Queue, Empty
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
import tkinter as tk
from tkinter import messagebox, scrolledtext


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
DEFAULT_OUTPUT_DIR_NAME = "FetchedBibliographyPDFs"


def ensure_output_directory(base_dir: Path) -> Path:
    """Ensure the directory used to store PDFs exists and return its path."""

    output_dir = base_dir / DEFAULT_OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def find_doi(text: str) -> Optional[str]:
    """Return the first DOI found in *text*, if any."""

    match = DOI_PATTERN.search(text)
    if match:
        return match.group(0).rstrip(".;)")
    return None


def sanitize_filename(text: str, max_length: int = 80) -> str:
    """Create a safe filename based on the citation text."""

    cleaned = re.sub(r"[^A-Za-z0-9\-_ ]", "", text).strip()
    if not cleaned:
        cleaned = "citation"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned or "citation"


def read_bibliography_entries(text: str) -> Iterable[str]:
    """Split the bibliography text into individual entries."""

    entries: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        striped = line.strip()
        if not striped:
            if current:
                entries.append(" ".join(current))
                current = []
            continue
        current.append(striped)

    if current:
        entries.append(" ".join(current))

    return entries


def contact_email() -> str:
    return os.environ.get("CROSSREF_CONTACT_EMAIL", "email@example.com")


def request_headers(accept: str = "application/pdf, application/json;q=0.9, */*;q=0.8") -> dict[str, str]:
    contact = contact_email()
    return {
        "User-Agent": f"FetchLiterature/1.1 (mailto:{contact})",
        "Accept": accept,
    }


def download_pdf_from_url(url: str, destination: Path) -> bool:
    """Download *url* to *destination* if it serves a PDF."""

    try:
        response = requests.get(url, headers=request_headers(), timeout=30, stream=True)
        response.raise_for_status()
    except requests.RequestException:
        return False

    content_type = response.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not response.url.lower().endswith(".pdf"):
        return False

    with destination.open("wb") as file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)

    return True


def extract_pdf_url_from_html(html: str, base_url: str) -> Optional[str]:
    """Attempt to locate a PDF URL within HTML content."""

    meta_match = re.search(
        r"<meta[^>]+name=[\"']citation_pdf_url[\"'][^>]+content=[\"']([^\"']+)",
        html,
        re.IGNORECASE,
    )
    if meta_match:
        return urljoin(base_url, meta_match.group(1))

    meta_generic = re.search(
        r"<meta[^>]+content=[\"']([^\"']+\.pdf)[\"'][^>]*>",
        html,
        re.IGNORECASE,
    )
    if meta_generic:
        return urljoin(base_url, meta_generic.group(1))

    anchor_match = re.search(
        r"<a[^>]+href=[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"'][^>]*>",
        html,
        re.IGNORECASE,
    )
    if anchor_match:
        return urljoin(base_url, anchor_match.group(1))

    script_match = re.search(
        r"\"pdfUrl\"\s*:\s*\"([^\"']+\.pdf)\"",
        html,
        re.IGNORECASE,
    )
    if script_match:
        return urljoin(base_url, script_match.group(1))

    return None


def resolve_doi_to_pdf(doi: str, destination: Path) -> bool:
    """Attempt to resolve a DOI and download the PDF."""

    doi_url = f"https://doi.org/{doi}"

    try:
        response = requests.get(doi_url, headers=request_headers(), timeout=30, stream=True)
        response.raise_for_status()
    except requests.RequestException:
        return False

    content_type = response.headers.get("Content-Type", "")
    if "pdf" in content_type.lower() or response.url.lower().endswith(".pdf"):
        with destination.open("wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)
        return True

    # If the DOI resolved to a landing page, attempt to follow any direct PDF link.
    if "text/html" in content_type.lower():
        html = response.content.decode(errors="replace")
        pdf_url = extract_pdf_url_from_html(html, response.url)
        if not pdf_url:
            pdf_url = response.headers.get("Location")
        if pdf_url:
            return download_pdf_from_url(pdf_url, destination)

    return False


def crossref_pdf_lookup(citation: str) -> Optional[str]:
    """Search Crossref for a citation and return a PDF URL if available."""

    try:
        response = requests.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": citation, "rows": 1},
            headers=request_headers("application/json, */*;q=0.8"),
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    try:
        items = response.json()["message"].get("items", [])
    except (ValueError, KeyError):
        return None

    if not items:
        return None

    links = items[0].get("link", [])
    for link in links:
        if link.get("content-type", "").lower() == "application/pdf" and link.get("URL"):
            return link["URL"]

    return None


def unpaywall_pdf_lookup(doi: str) -> Optional[str]:
    """Query the Unpaywall API for an open-access PDF URL."""

    email = contact_email()
    try:
        response = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email},
            headers=request_headers("application/json, */*;q=0.8"),
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    try:
        data = response.json()
    except ValueError:
        return None

    locations = []
    if data.get("best_oa_location"):
        locations.append(data["best_oa_location"])
    locations.extend(data.get("oa_locations", []) or [])

    for location in locations:
        pdf_url = location.get("url_for_pdf") or location.get("url")
        if pdf_url:
            return pdf_url

    return None


class FetcherApp:
    """Tkinter-based GUI for fetching PDFs from bibliography entries."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Fetch Literature PDFs")

        self.output_directory = ensure_output_directory(Path.home() / "Desktop")

        self.text_area = scrolledtext.ScrolledText(self.root, width=100, height=25)
        self.text_area.pack(padx=10, pady=(10, 5), fill=tk.BOTH, expand=True)

        self.download_button = tk.Button(
            self.root, text="Download PDFs", command=self.start_download_thread
        )
        self.download_button.pack(pady=5)

        self.log_area = scrolledtext.ScrolledText(self.root, width=100, height=10, state=tk.DISABLED)
        self.log_area.pack(padx=10, pady=(5, 10), fill=tk.BOTH, expand=True)

        self.log_queue: Queue[str] = Queue()
        self.root.after(100, self.process_log_queue)

    def log(self, message: str) -> None:
        """Add a message to the log area in a thread-safe manner."""

        self.log_queue.put(message)

    def process_log_queue(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_area.configure(state=tk.NORMAL)
                self.log_area.insert(tk.END, message + "\n")
                self.log_area.configure(state=tk.DISABLED)
                self.log_area.yview_moveto(1.0)
        except Empty:
            pass
        finally:
            self.root.after(100, self.process_log_queue)

    def start_download_thread(self) -> None:
        bibliography = self.text_area.get("1.0", tk.END).strip()
        if not bibliography:
            messagebox.showinfo("Fetch Literature PDFs", "Please paste a bibliography first.")
            return

        self.download_button.config(state=tk.DISABLED)
        thread = threading.Thread(target=self.download_bibliography, args=(bibliography,), daemon=True)
        thread.start()

    def download_bibliography(self, bibliography: str) -> None:
        entries = list(read_bibliography_entries(bibliography))

        if not entries:
            self.log("No citations detected. Paste entries separated by blank lines.")
            self.download_button.config(state=tk.NORMAL)
            return

        self.log(f"Found {len(entries)} citation(s). Saving to {self.output_directory}")

        for index, citation in enumerate(entries, start=1):
            safe_name = sanitize_filename(citation)
            destination = self.output_directory / f"{index:02d}_{safe_name}.pdf"

            self.log(f"[{index}/{len(entries)}] Processing citation: {citation}")

            doi = find_doi(citation)
            if doi:
                if resolve_doi_to_pdf(doi, destination):
                    self.log(f"  ✓ Downloaded via DOI {doi}")
                    continue
                else:
                    self.log(f"  ⚠️ Unable to download PDF directly from DOI {doi}")

                unpaywall_url = unpaywall_pdf_lookup(doi)
                if unpaywall_url and download_pdf_from_url(unpaywall_url, destination):
                    self.log("  ✓ Downloaded via Unpaywall open access link")
                    continue
                elif unpaywall_url:
                    self.log("  ⚠️ Unpaywall provided a link but download failed")

            pdf_url = crossref_pdf_lookup(citation)
            if pdf_url and download_pdf_from_url(pdf_url, destination):
                self.log(f"  ✓ Downloaded PDF from Crossref link")
                continue

            self.log("  ✗ Failed to retrieve PDF. Check access manually.")

        self.log("Download attempt complete.")
        self.download_button.config(state=tk.NORMAL)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = FetcherApp()
    app.run()


if __name__ == "__main__":
    main()
