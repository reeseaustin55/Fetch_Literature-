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
import shutil
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from queue import Queue, Empty
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
DEFAULT_OUTPUT_DIR_NAME = "FetchedBibliographyPDFs"


def ensure_output_directory(base_dir: Path) -> Path:
    """Ensure the directory used to store PDFs exists and return its path."""

    output_dir = base_dir / DEFAULT_OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _windows_download_directory() -> Optional[Path]:
    """Try to locate the Downloads folder on Windows systems."""

    if os.name != "nt":
        return None

    # Newer Windows versions expose the GUID key; older ones have "Downloads".
    candidates = (
        "{374DE290-123F-4565-9164-39C4925E467B}",
        "Downloads",
    )

    try:
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            for name in candidates:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                expanded = os.path.expandvars(value)
                path = Path(expanded)
                if path.exists():
                    return path
    except Exception:
        # Fall back to environment heuristics if registry lookup fails.
        pass

    profile = os.environ.get("USERPROFILE")
    if profile:
        candidate = Path(profile) / "Downloads"
        if candidate.exists():
            return candidate

    return None


def _xdg_download_directory() -> Optional[Path]:
    """Locate the XDG-compliant download directory on POSIX systems."""

    if os.name == "nt":
        return None

    config_file = Path.home() / ".config" / "user-dirs.dirs"
    if not config_file.exists():
        return None

    try:
        content = config_file.read_text(encoding="utf8")
    except OSError:
        return None

    match = re.search(r"XDG_DOWNLOAD_DIR\s*=\s*\"([^\"]+)\"", content)
    if not match:
        return None

    raw_path = match.group(1)
    # Replace environment variables like $HOME.
    expanded = os.path.expandvars(raw_path)
    expanded = expanded.replace("$HOME", str(Path.home()))
    path = Path(expanded).expanduser()
    if path.exists():
        return path

    return None


def get_download_directory() -> Optional[Path]:
    """Best effort attempt to find the user's default downloads folder."""

    path = _windows_download_directory()
    if path:
        return path

    path = _xdg_download_directory()
    if path:
        return path

    fallback = Path.home() / "Downloads"
    if fallback.exists():
        return fallback

    return None


def snapshot_pdf_files(directory: Path) -> dict[Path, tuple[float, int]]:
    """Return a mapping of PDF paths to their (mtime, size) snapshot."""

    snapshot: dict[Path, tuple[float, int]] = {}
    try:
        files = list(directory.glob("*.pdf"))
    except OSError:
        return snapshot

    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[path.resolve()] = (stat.st_mtime, stat.st_size)
    return snapshot


def detect_new_pdf(
    directory: Path,
    snapshot: dict[Path, tuple[float, int]],
    start_time: float,
) -> Optional[Path]:
    """Return a newly downloaded PDF that appeared after *start_time*."""

    try:
        candidates = sorted(
            directory.glob("*.pdf"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue

        if stat.st_mtime < start_time:
            continue

        resolved = path.resolve()
        previous = snapshot.get(resolved)
        if previous and stat.st_mtime <= previous[0] and stat.st_size == previous[1]:
            continue

        # Ensure the file is ready by trying to open it briefly.
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError:
            continue

        snapshot[resolved] = (stat.st_mtime, stat.st_size)
        return path

    return None


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


def extract_urls(text: str) -> list[str]:
    """Return HTTP(S) URLs embedded in *text*."""

    urls = []
    for match in URL_PATTERN.findall(text):
        url = match.rstrip(".,);")
        urls.append(url)
    return urls


ENTRY_START_PATTERN = re.compile(
    r"^\s*(?:\(\d+\)|\[\d+\]|\d+[.)])\s*",
    re.MULTILINE,
)


def read_bibliography_entries(text: str) -> Iterable[str]:
    """Split the bibliography text into individual entries."""

    entries: list[str] = []
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            if current:
                entries.append(" ".join(current))
                current = []
            continue

        if current and ENTRY_START_PATTERN.match(line):
            entries.append(" ".join(current))
            current = []

        current.append(line)

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


@dataclass
class ManualDownloadRequest:
    """Instruction sent from the worker thread to the UI for manual handling."""

    index: int
    total: int
    citation: str
    destination: Path
    candidate_urls: list[str]
    response_queue: Queue[bool]


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
        self.manual_queue: Queue[ManualDownloadRequest] = Queue()
        self.root.after(100, self.process_log_queue)
        self.root.after(150, self.process_manual_queue)

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

            manual_urls: list[str] = []
            if doi:
                manual_urls.append(f"https://doi.org/{doi}")
            if pdf_url:
                manual_urls.append(pdf_url)
            manual_urls.extend(url for url in extract_urls(citation) if url not in manual_urls)

            if manual_urls:
                self.log("  ⏳ Manual download required. Opening browser...")
                if self.request_manual_download(index, len(entries), citation, destination, manual_urls):
                    self.log("  ✓ Added manual download to folder")
                    continue
                else:
                    self.log("  ⚠️ Manual download skipped or file not selected")

            self.log("  ✗ Failed to retrieve PDF. Check access manually.")

        self.log("Download attempt complete.")
        self.download_button.config(state=tk.NORMAL)

    def run(self) -> None:
        self.root.mainloop()

    def request_manual_download(
        self,
        index: int,
        total: int,
        citation: str,
        destination: Path,
        candidate_urls: list[str],
    ) -> bool:
        response_queue: Queue[bool] = Queue(maxsize=1)
        request = ManualDownloadRequest(
            index=index,
            total=total,
            citation=citation,
            destination=destination,
            candidate_urls=candidate_urls,
            response_queue=response_queue,
        )
        self.manual_queue.put(request)
        try:
            return response_queue.get()
        except Exception:
            return False

    def process_manual_queue(self) -> None:
        try:
            request = self.manual_queue.get_nowait()
        except Empty:
            pass
        else:
            self.handle_manual_request(request)
        finally:
            self.root.after(150, self.process_manual_queue)

    def handle_manual_request(self, request: ManualDownloadRequest) -> None:
        if request.candidate_urls:
            # Open the first URL automatically.
            webbrowser.open_new_tab(request.candidate_urls[0])

        dialog = tk.Toplevel(self.root)
        dialog.title("Manual PDF Download Required")
        dialog.transient(self.root)
        dialog.grab_set()

        message = (
            "Automatic download failed. A browser tab has been opened so you can "
            "retrieve the PDF manually. Once the PDF finishes downloading, it will "
            "be captured automatically and copied into the bibliography folder. "
            "If automatic capture fails you can still browse for the file manually."
        )

        tk.Label(dialog, text=message, wraplength=500, justify=tk.LEFT).pack(padx=15, pady=(15, 10))

        if len(request.candidate_urls) > 1:
            tk.Label(dialog, text="Useful links:", font=("TkDefaultFont", 9, "bold")).pack(anchor="w", padx=15)
            for url in request.candidate_urls:
                link = tk.Label(dialog, text=url, fg="blue", cursor="hand2", wraplength=500, justify=tk.LEFT)
                link.pack(anchor="w", padx=25)
                link.bind("<Button-1>", lambda _event, target=url: webbrowser.open_new_tab(target))

        downloads_dir = get_download_directory()
        status_var = tk.StringVar()
        snapshot: dict[Path, tuple[float, int]] = {}
        start_time = time.time()
        poll_state = {"active": True, "notified": False}

        def copy_to_destination(source: Path) -> bool:
            destination = request.destination
            if source.suffix and source.suffix.lower() != destination.suffix.lower():
                destination = destination.with_suffix(source.suffix)

            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            except OSError as exc:
                messagebox.showerror(
                    "Fetch Literature PDFs",
                    f"Unable to copy the downloaded file into the output folder:\n{exc}",
                )
                return False

            return True

        if downloads_dir and downloads_dir.exists():
            snapshot = snapshot_pdf_files(downloads_dir)
            downloads_display = str(downloads_dir)
            status_var.set(
                f"Waiting for a PDF saved to {downloads_display}. Keep this dialog open until it is captured."
            )
        else:
            status_var.set(
                "Downloads folder could not be located automatically. Use the Browse button below when ready."
            )

        status_label = tk.Label(dialog, textvariable=status_var, wraplength=500, justify=tk.LEFT)
        status_label.pack(padx=15, pady=(0, 10), anchor="w")

        button_frame = tk.Frame(dialog)
        button_frame.pack(fill=tk.X, padx=15, pady=(10, 15))

        def finish(result: bool) -> None:
            if not request.response_queue.full():
                request.response_queue.put(result)
            poll_state["active"] = False
            dialog.destroy()

        def complete_with_file(file_path: Path) -> None:
            if not copy_to_destination(file_path):
                return

            if status_var.get():
                status_var.set(f"Captured {file_path.name} and copied it into the output folder.")

            finish(True)

        def on_select_file() -> None:
            file_path = filedialog.askopenfilename(
                title="Select downloaded PDF",
                filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            )
            if not file_path:
                return

            selected = Path(file_path)
            if not selected.exists():
                messagebox.showerror("Fetch Literature PDFs", "The selected file does not exist.")
                return

            complete_with_file(selected)

        def on_skip() -> None:
            finish(False)

        select_button = tk.Button(button_frame, text="Browse for PDF...", command=on_select_file)
        select_button.pack(side=tk.LEFT)

        skip_button = tk.Button(button_frame, text="Skip", command=on_skip)
        skip_button.pack(side=tk.RIGHT)

        dialog.protocol("WM_DELETE_WINDOW", on_skip)

        if downloads_dir and downloads_dir.exists():
            timeout_seconds = 300

            def poll_for_download() -> None:
                if not poll_state["active"]:
                    return

                new_file = detect_new_pdf(downloads_dir, snapshot, start_time)
                if new_file:
                    complete_with_file(new_file)
                    return

                elapsed = time.time() - start_time
                if elapsed > timeout_seconds and not poll_state["notified"]:
                    status_var.set(
                        "No new PDF detected yet. Keep the dialog open, or use the Browse button to select the file manually."
                    )
                    poll_state["notified"] = True

                dialog.after(1000, poll_for_download)

            dialog.after(1500, poll_for_download)


def main() -> None:
    app = FetcherApp()
    app.run()


if __name__ == "__main__":
    main()
