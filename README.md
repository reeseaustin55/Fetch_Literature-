# Fetch Literature PDFs

This repository contains a Python script that automates the retrieval of PDF
files referenced in a bibliography.  Given a text file with one reference per
line (see `sample_bibliography.txt` for an example), the script uses Selenium to
open each DOI/URL in Chrome, triggers the PDF download, and moves the resulting
file into a folder on the current user's Desktop.

## Prerequisites

* Python 3.9+
* Google Chrome or Chromium installed locally
* ChromeDriver that matches the installed browser version
* The `selenium` Python package (`pip install selenium`)
* Access rights to the target publishers (log in through the Chrome profile if
  required)

## Usage

1. Save your bibliography as a UTF-8 text file with one reference per line. The
   script extracts the first DOI or URL from each line.
2. (Optional) Sign in to the relevant publisher websites using the Chrome
   profile you plan to reuse for Selenium automation.
3. Run the script:

   ```bash
   python fetch_pdfs.py sample_bibliography.txt \
       --folder-name "Atomic_Force_Papers" \
       --temp-download-dir "~/Downloads/selenium_pdf_temp" \
       --chrome-profile "~/Library/Application Support/Google/Chrome"
   ```

   * `--folder-name` controls the destination folder created on the Desktop.
   * `--temp-download-dir` overrides the temporary download directory.
   * `--headless` runs Chrome without a visible window (requires Chrome 109+).
   * `--chrome-profile` points to an existing Chrome user data directory so the
     automated browser session inherits cookies and institution access.

4. The downloaded PDFs are renamed using the reference labels and saved in the
   Desktop folder specified.

## Troubleshooting

* Some publisher websites require additional navigation before a PDF becomes
  available.  When the script cannot find a PDF download control it raises an
  error for that entry and continues with the next reference.
* You can rerun the script after manually downloading tricky articles; the
  script skips references whose DOI/URL has already been processed.

## Running in PyCharm

1. Open this repository in PyCharm.
2. Create a run configuration that executes `fetch_pdfs.py` and passes the path
   to your bibliography file as the first argument.
3. Ensure that PyCharm's Python interpreter has access to Selenium and that the
   ChromeDriver binary is discoverable via `PATH`.

