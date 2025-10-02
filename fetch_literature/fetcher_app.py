"""Tkinter GUI for assisting with literature PDF downloads."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if __package__:
    from .browser_automation import (
        AutomationResult,
        attempt_automated_pdf_download,
        ensure_playwright_setup,
    )
else:  # When executed as a stand-alone script.
    from fetch_literature.browser_automation import (
        AutomationResult,
        attempt_automated_pdf_download,
        ensure_playwright_setup,
    )

logger = logging.getLogger(__name__)


@dataclass
class FetcherConfig:
    """Runtime configuration for :class:`FetcherApp`."""

    default_download_dir: Path = Path.home() / "Downloads"
    automation_default: bool = False


class FetcherApp:
    """Tkinter front-end for coordinating bibliography downloads."""

    def __init__(self, root: tk.Tk | tk.Toplevel, config: FetcherConfig | None = None) -> None:
        self.root = root
        self.config = config or FetcherConfig()

        self.url_var = tk.StringVar()
        self.download_dir_var = tk.StringVar(value=str(self.config.default_download_dir))
        self.automation_enabled = tk.BooleanVar(value=self.config.automation_default)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction helpers
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title("Fetch Literature")

        container = ttk.Frame(self.root, padding=12)
        container.grid(column=0, row=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        ttk.Label(container, text="Article URL:").grid(column=0, row=0, sticky="w")
        ttk.Entry(container, textvariable=self.url_var, width=60).grid(
            column=1, row=0, columnspan=2, sticky="ew", padx=(8, 0)
        )
        container.columnconfigure(1, weight=1)

        ttk.Label(container, text="Downloads folder:").grid(column=0, row=1, sticky="w", pady=(8, 0))
        ttk.Entry(container, textvariable=self.download_dir_var, width=60).grid(
            column=1, row=1, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        ttk.Button(container, text="Browse…", command=self._prompt_download_directory).grid(
            column=2, row=1, padx=(8, 0), pady=(8, 0)
        )

        ttk.Checkbutton(
            container,
            text="Attempt automatic PDF download (requires Playwright)",
            variable=self.automation_enabled,
        ).grid(column=0, row=2, columnspan=3, sticky="w", pady=(12, 0))

        ttk.Button(
            container,
            text="Prepare automation dependencies",
            command=self._setup_automation_dependencies,
        ).grid(column=0, row=3, columnspan=3, sticky="w", pady=(12, 0))

        ttk.Button(container, text="Download bibliography", command=self.download_bibliography).grid(
            column=0, row=4, columnspan=3, pady=(16, 0)
        )

        ttk.Label(container, textvariable=self.status_var, foreground="#444444").grid(
            column=0, row=5, columnspan=3, sticky="w", pady=(12, 0)
        )

    # ------------------------------------------------------------------
    # Core behaviour
    # ------------------------------------------------------------------
    def download_bibliography(self) -> Optional[Path]:
        """Download the bibliography PDF using automation when available."""

        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Please enter the article URL before downloading.")
            return None

        download_dir = Path(self.download_dir_var.get()).expanduser().resolve()
        download_dir.mkdir(parents=True, exist_ok=True)

        logger.debug("Initiating download for %s", url)

        if self.automation_enabled.get():
            result: AutomationResult = attempt_automated_pdf_download(
                candidate_url=url,
                download_dir=download_dir,
            )
            if result.path:
                self.status_var.set(f"Downloaded automatically: {result.path}")
                messagebox.showinfo("Download complete", f"Automatically downloaded PDF to {result.path}")
                return result.path

            if result.attempted:
                details = result.error or "Unable to locate a PDF link automatically."
                messagebox.showwarning(
                    "Automatic download unavailable",
                    f"The automated browser could not complete the download.\n\n{details}\n"
                    "Please use the manual file picker instead.",
                )
            elif result.error:
                messagebox.showinfo(
                    "Automation not available",
                    f"Automatic downloads were skipped because: {result.error}\n"
                    "The manual file picker will be shown instead.",
                )

        selected_path = self.request_manual_download(download_dir)
        if selected_path:
            self.status_var.set(f"Using manually selected file: {selected_path}")
        else:
            self.status_var.set("No PDF selected yet")
        return selected_path

    def request_manual_download(self, download_dir: Path) -> Optional[Path]:
        """Prompt the user to manually choose a downloaded bibliography file."""

        file_path = filedialog.askopenfilename(
            parent=self.root,
            title="Select the downloaded bibliography PDF",
            initialdir=str(download_dir),
            filetypes=[("PDF files", "*.pdf"), ("All files", "*")],
        )
        if not file_path:
            return None

        pdf_path = Path(file_path)
        messagebox.showinfo("Bibliography selected", f"Using {pdf_path}")
        return pdf_path

    # ------------------------------------------------------------------
    # Helper callbacks
    # ------------------------------------------------------------------
    def _prompt_download_directory(self) -> None:
        current = Path(self.download_dir_var.get()).expanduser()
        chosen = filedialog.askdirectory(parent=self.root, initialdir=str(current), title="Select downloads folder")
        if chosen:
            self.download_dir_var.set(chosen)

    def _setup_automation_dependencies(self) -> None:
        """Install Playwright and the headless browser without leaving the app."""

        report = ensure_playwright_setup()
        for entry in report.logs:
            logger.info("Automation setup: %s", entry)

        if report.succeeded:
            messagebox.showinfo(
                "Automation ready",
                "Playwright and the Chromium browser are installed for this interpreter.",
            )
            self.status_var.set("Automation dependencies prepared")
        else:
            error = report.error or "An unknown error prevented installation."
            messagebox.showerror(
                "Automation setup failed",
                "Could not prepare the automation environment.\n\n"
                f"Details:\n{error}\n\nSee the application logs for the full transcript.",
            )
            self.status_var.set("Automation setup encountered an error")


def main() -> None:
    """Launch the Fetcher GUI."""

    root = tk.Tk()
    FetcherApp(root)
    root.mainloop()


__all__ = ["FetcherApp", "FetcherConfig", "main"]


if __name__ == "__main__":  # pragma: no cover - GUI entry point
    main()
