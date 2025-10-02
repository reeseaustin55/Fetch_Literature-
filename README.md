## Fetch Literature PDFs

This project provides a desktop-friendly Python script that automates the process of collecting PDF files from a list of bibliographic references. Paste your bibliography into the window, choose a download directory, and the script will open each DOI in Microsoft Edge (or Google Chrome) to click through to the PDF and download it for you.

### Features

* **GUI workflow** – paste bibliographic text, choose download directory, and pick the browser.
* **Automatic DOI extraction** – DOIs are detected with a regular expression (duplicates removed).
* **Browser automation** – uses Selenium WebDriver to open each DOI page, find PDF links, and download the files to your chosen folder.
* **Download monitoring** – waits for each PDF to finish downloading and reports success or failure in the GUI log.

### Requirements

* Python 3.9+
* Google Chrome **or** Microsoft Edge (Edge is the default)
* The script automatically manages the appropriate WebDriver binaries via [`webdriver-manager`](https://github.com/SergeyPirogov/webdriver_manager).

Install Python dependencies:

```bash
pip install -r requirements.txt
```

### Running the Script

```bash
python main.py
```

1. Paste or type your bibliography entries into the text box.
2. Optional – click **Browse** to change the download directory (defaults to your Desktop).
3. Choose **Edge** or **Chrome** from the browser list.
4. Click **Download PDFs**.

Status updates and any issues encountered while visiting DOIs are displayed in the GUI log.

### Notes

* Some publishers require institutional access, SSO, or captchas. In those cases, the automated browser may pause on the publisher’s login page. Resolve the access prompt manually and resume the download; the script keeps the browser open until all DOIs are processed.
* The script searches for buttons or links that reference PDFs. If a site uses an uncommon download flow the PDF might not be detected automatically. Use the GUI log to identify entries that need manual attention.

