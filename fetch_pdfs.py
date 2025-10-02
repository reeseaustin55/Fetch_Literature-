"""Fetch PDFs for bibliography entries using the Crossref API.

This script looks up each citation through Crossref and tries to download an
available PDF.  It saves the PDFs to the given directory.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
from typing import List, Optional

import requests

# Crossref asks that automated clients provide a descriptive User-Agent header
# that includes contact information.  Update this string to include your email
# address or institutional contact details.
DEFAULT_USER_AGENT = (
    "FetchLiteratureBot/1.0 (mailto:your-email@example.com)"
)

PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
}

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Look up citations using the Crossref API and download available PDFs. "
            "Provide a text file containing the bibliography or pipe the text via stdin."
        )
    )
    parser.add_argument(
        "bibliography",
        nargs="?",
        help="Path to a file that contains the bibliography text. If omitted, stdin is used.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path("downloads"),
        help="Directory to store downloaded PDFs (default: ./downloads).",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help=(
            "User-Agent string to send to Crossref. Include your email address as per Crossref's etiquette."
        ),
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to sleep between Crossref requests to avoid rate limiting (default: 1).",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1,
        help="Number of Crossref matches to inspect for each citation (default: 1).",
    )
    return parser.parse_args(argv)


def read_bibliography_text(path: Optional[str]) -> str:
    if path:
        return pathlib.Path(path).read_text(encoding="utf-8")
    return sys.stdin.read()


def split_entries(text: str) -> List[str]:
    # Split on blank lines, but also handle numbered lists such as "1. ...".
    text = text.strip()
    if not text:
        return []

    # Replace numbered prefixes with blank lines to aid splitting.
    cleaned = re.sub(r"\n\s*\d+\.\s+", "\n\n", text)
    parts = re.split(r"\n\s*\n", cleaned)
    entries = [re.sub(r"\s+", " ", part).strip() for part in parts if part.strip()]
    return entries


def crossref_lookup(
    citation: str,
    *,
    session: requests.Session,
    user_agent: str,
    max_results: int,
) -> List[dict]:
    """Return candidate Crossref works for a citation."""
    url = "https://api.crossref.org/works"
    email_match = EMAIL_RE.search(user_agent)
    mailto = email_match.group(0) if email_match else None
    params = {
        "query.bibliographic": citation,
        "rows": max(1, max_results),
        "select": "title,DOI,author,issued,link",
    }
    if mailto:
        params["mailto"] = mailto
    headers = {"User-Agent": user_agent}
    response = session.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    message = response.json().get("message", {})
    return message.get("items", [])


def choose_pdf_link(work: dict) -> Optional[str]:
    links = work.get("link") or []
    for link in links:
        content_type = (link.get("content-type") or "").lower()
        if content_type in PDF_CONTENT_TYPES:
            return link.get("URL")
    return None


def fetch_pdf(
    url: str,
    *,
    session: requests.Session,
    user_agent: str,
    accept_header: Optional[str] = None,
) -> Optional[bytes]:
    headers = {"User-Agent": user_agent}
    if accept_header:
        headers["Accept"] = accept_header

    response = session.get(url, headers=headers, timeout=60, allow_redirects=True)
    if response.status_code == 200:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
        if content_type in PDF_CONTENT_TYPES:
            return response.content
    return None


def safe_filename(title: str, doi: Optional[str]) -> str:
    if title:
        base = re.sub(r"[^A-Za-z0-9-_]+", "_", title).strip("_")
    elif doi:
        base = re.sub(r"[^A-Za-z0-9-_]+", "_", doi).strip("_")
    else:
        base = "citation"

    return base[:150] or "citation"


def download_for_entry(
    entry: str,
    *,
    session: requests.Session,
    output_dir: pathlib.Path,
    user_agent: str,
    max_results: int,
) -> str:
    works = crossref_lookup(
        entry, session=session, user_agent=user_agent, max_results=max_results
    )
    if not works:
        return "No Crossref match found."

    last_error: Optional[str] = None
    for work in works:
        title_parts = work.get("title") or []
        title = title_parts[0] if title_parts else ""
        doi = work.get("DOI")
        link_url = choose_pdf_link(work)

        if link_url:
            pdf = fetch_pdf(link_url, session=session, user_agent=user_agent)
            if pdf:
                name = safe_filename(title, doi)
                path = output_dir / f"{name}.pdf"
                path.write_bytes(pdf)
                return f"Downloaded via Crossref link as {path.name}."
            last_error = "Crossref-provided PDF link was not accessible."
            continue

        if doi:
            doi_url = f"https://doi.org/{doi}"
            pdf = fetch_pdf(
                doi_url,
                session=session,
                user_agent=user_agent,
                accept_header="application/pdf",
            )
            if pdf:
                name = safe_filename(title, doi)
                path = output_dir / f"{name}.pdf"
                path.write_bytes(pdf)
                return f"Downloaded via DOI redirect as {path.name}."
            last_error = "DOI did not resolve directly to a PDF."
            continue

        last_error = "No PDF link or DOI available."

    return last_error or "Unable to retrieve PDF."


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    bibliography_text = read_bibliography_text(args.bibliography)
    entries = split_entries(bibliography_text)
    if not entries:
        print("No bibliography entries found.")
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})

    for index, entry in enumerate(entries, start=1):
        print(f"[{index}/{len(entries)}] {entry}")
        try:
            result = download_for_entry(
                entry,
                session=session,
                output_dir=args.output_dir,
                user_agent=args.user_agent,
                max_results=args.max_results,
            )
            print(f"    -> {result}")
        except requests.HTTPError as exc:
            print(f"    -> HTTP error: {exc}")
        except requests.RequestException as exc:
            print(f"    -> Request failed: {exc}")
        finally:
            time.sleep(max(0.0, args.sleep))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
