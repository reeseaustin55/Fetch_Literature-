# Fetch Literature PDFs

This small utility provides a Tkinter interface that lets you paste in the
bibliography from a paper and download the associated PDFs. The application
stores the downloaded documents inside a folder named
`FetchedBibliographyPDFs` on your Desktop.

## Requirements

- Python 3.9 or newer
- The `requests` package (install via `pip install requests`)

## Running the app

```bash
python fetch_pdfs.py
```

When the window opens:

1. Paste the bibliography into the large text box. Citations can be separated
   by blank lines or standard numbering such as `(1)`, `[2]`, or `3.` at the
   start of each entry.
2. Click **Download PDFs**.
3. Monitor progress in the log at the bottom of the window.

If a PDF cannot be downloaded automatically the app opens your default
browser to the best available landing page and shows a dialog. Download the
PDF in the browser and leave the dialog open—the app watches your system
Downloads folder and, as soon as a new PDF appears, it is copied into the
`FetchedBibliographyPDFs` folder so the queue can continue. If the automatic
capture misses a file, click **Browse for PDF...** in the dialog and point the
tool at the correct download manually.

## How it works

For each citation the script tries the following approaches:

1. **Direct DOI resolution** – If a DOI is present, the script attempts to
   resolve it via `https://doi.org/`. If a landing page is returned instead of a
   PDF, the HTML is scanned for common PDF link hints (e.g.,
   `citation_pdf_url` meta tags and direct `.pdf` anchors).
2. **Unpaywall open access lookup** – When direct resolution fails, the script
   queries the Unpaywall API for an open-access PDF link associated with the
   DOI.
3. **Crossref lookup** – If no DOI is detected or prior attempts fail, the
   script queries the Crossref `query.bibliographic` endpoint and uses any
   advertised PDF links.

Set the `CROSSREF_CONTACT_EMAIL` environment variable to your email address so
that polite contact information is included in Crossref and Unpaywall requests:

```bash
export CROSSREF_CONTACT_EMAIL="you@example.edu"
```

> **Note:** Some publishers require authentication via your institution. When
> that happens, sign in within the opened browser tab. The app will notice the
> completed download in your Downloads folder and move it into the output
> directory automatically, keeping the rest of the queue moving.

