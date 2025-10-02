# Fetch Literature PDFs

This repository contains tools that read a bibliography and automatically
download available PDF files for the cited works. You can run the downloader
either from the command line or through a small graphical interface for easy
pasting.

## Features
- Splits a pasted bibliography into individual citations.
- Queries the Crossref API to locate DOIs and publisher links.
- Downloads PDFs from Crossref-provided links or DOI redirects when available.
- Stores the files in a local directory for offline use.

## Requirements
- Python 3.9 or newer.
- The [`requests`](https://docs.python-requests.org) library.

Install the dependency with:

```bash
pip install -r requirements.txt
```

## Usage
1. Prepare a text file (e.g., `bibliography.txt`) that contains the references
   you want to download. You can also paste the bibliography directly into the
   terminal by using standard input.
2. Run the script:

   - If you execute `python fetch_pdfs.py` without additional arguments and
     there is no data coming from standard input, the script opens the GUI
     automatically (provided Tkinter is available) so you can paste the
     bibliography and monitor download status messages.
   - To stay in the terminal, provide a file path or pipe the bibliography via
     standard input:

```bash
python fetch_pdfs.py bibliography.txt
```

Or, to paste the bibliography directly:

```bash
python fetch_pdfs.py <<'END'
Author, A. A. (2020). *Sample article*. Journal Name.
...
END
```

### Graphical interface
Launch the GUI manually when desired (for example, if your environment does not
allow the script to auto-detect whether standard input has data):

```bash
python fetch_pdfs.py --gui
```

Within the GUI you can:

- Paste references into the large text box.
- Choose the download directory with the “Browse…” button.
- Customize the User-Agent string (remember to include your email address).
- Monitor status updates for each citation, including messages when a PDF could
  not be located.

### Useful options
- `-o, --output-dir`: Directory where PDFs are saved (default: `downloads`).
- `--user-agent`: Custom User-Agent header. Include your institutional email
  address to comply with Crossref's etiquette (e.g.,
  `"MyScript/1.0 (mailto:me@university.edu)"`).
- `--sleep`: Seconds to pause between requests (default: `1`).
- `--max-results`: Number of Crossref candidates to inspect for each citation
  (default: `1`). Increase this if the top result is not always correct.

### Notes
- The script relies on Crossref metadata and publisher links. If a PDF is
  behind an institutional subscription, make sure you run the script from an
  environment that has access to the publisher's content.
- Not every citation returned by Crossref has a publicly accessible PDF. In
  those cases the script reports the reason for the failure and moves on.
- Update the `DEFAULT_USER_AGENT` string in `fetch_pdfs.py` with your real
  contact information before making heavy use of the tool.

## Development
Feel free to open pull requests with improvements. Remember to respect
Crossref's rate limits and terms of use.
