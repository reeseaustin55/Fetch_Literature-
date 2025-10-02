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

1. Paste the bibliography into the large text box. Separate citations with an
   empty line.
2. Click **Download PDFs**.
3. Monitor progress in the log at the bottom of the window.

## How it works

For each citation the script tries the following approaches:

1. **Direct DOI resolution** – If a DOI is present, the script attempts to
   resolve it via `https://doi.org/` and download the resulting PDF.
2. **Crossref lookup** – If no DOI is detected or the direct download fails,
   the script queries the Crossref `query.bibliographic` endpoint and uses any
   advertised PDF links.

Set the `CROSSREF_CONTACT_EMAIL` environment variable to your email address so
that polite contact information is included in Crossref requests:

```bash
export CROSSREF_CONTACT_EMAIL="you@example.edu"
```

> **Note:** Some publishers require authentication via your institution. If a
> PDF cannot be retrieved automatically you may need to download it manually.

