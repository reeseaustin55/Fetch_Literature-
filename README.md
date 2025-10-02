# Bibliography PDF Fetcher

This project provides a small desktop utility that accepts a bibliography and
automatically downloads the referenced PDFs using Selenium automation.  The
application is designed to be executed from PyCharm (or any Python
environment).

## Features

- Paste an entire bibliography into the GUI.
- Automatically extract DOI and HTTP links.
- Download PDFs through Microsoft Edge (default) or Google Chrome.
- Configure the download directory (defaults to the Desktop).
- View a running log of the automation process.

## Getting Started

1. **Create a virtual environment (recommended)**

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**

   ```bash
   python fetch_pdfs.py
   ```

When the GUI opens, paste your bibliography into the main text box, adjust the
download folder or browser if necessary, and click **Download PDFs**.

## Notes

- The script relies on `webdriver-manager` to automatically download the
  appropriate Edge or Chrome driver.  Ensure that the chosen browser is
  installed on your system.
- Access to full PDFs still depends on your institutional or personal
  subscription rights.  The script simply automates the same browser workflow
  you would otherwise perform manually.
- Download automation is best-effort—some publishers may use custom platforms
  that require additional authentication steps that cannot be automated.

## Troubleshooting

- If the automation cannot locate a PDF link for a specific entry, check the
  log panel for details.  You can visit the DOI manually to confirm whether the
  PDF is accessible.
- Some publishers may require cookies or additional authentication prompts that
  cannot be handled automatically.  Signing into the publisher site before
  starting the download process can help.
- If the wrong browser opens, ensure the browser selection in the GUI matches
  the installed browser and that any existing Selenium sessions have been
  closed.

## License

This project is provided as-is without any warranty.
