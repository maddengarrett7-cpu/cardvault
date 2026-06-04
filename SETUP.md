# Card Scanner — Setup Guide

## 1. Get an Anthropic API Key
1. Go to https://console.anthropic.com and sign up / log in.
2. Click **API Keys** in the left sidebar → **Create Key**.
3. Copy the key (starts with `sk-ant-…`).

## 2. Set up Google Sheets API
1. Go to https://console.cloud.google.com and create a new project (name it anything, e.g. "Card Scanner").
2. In the search bar type **"Sheets API"** → click **Google Sheets API** → **Enable**.
3. Go to **IAM & Admin → Service Accounts** → **Create Service Account**.
   - Name it anything (e.g. "card-scanner").
   - Skip the optional steps, click **Done**.
4. Click your new service account → **Keys** tab → **Add Key → Create new key → JSON**.
   - A file downloads. Rename it `google_creds.json` and move it to:
     `/Users/madden/Projects/card-scanner/google_creds.json`

## 3. Create & share your Google Sheet
1. Go to https://sheets.google.com and create a new spreadsheet.
2. Name the first tab `Cards` (click the tab at the bottom to rename).
3. Copy the Spreadsheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/**SPREADSHEET_ID**/edit`
4. Click **Share** (top right) and share the sheet with the service account email
   (looks like `card-scanner@your-project.iam.gserviceaccount.com`) with **Editor** access.

## 4. Install Python dependencies
Open Terminal and run:
```bash
cd /Users/madden/Projects/card-scanner
pip3 install -r requirements.txt
```

## 5. Run the scanner
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export SPREADSHEET_ID="your-sheet-id-here"
python3 card_scanner.py
```

Or create a launcher script so you don't have to type the keys each time:

```bash
# Save as run.sh in the card-scanner folder
export ANTHROPIC_API_KEY="sk-ant-..."
export SPREADSHEET_ID="your-sheet-id-here"
python3 /Users/madden/Projects/card-scanner/card_scanner.py
```
Then run it with: `bash /Users/madden/Projects/card-scanner/run.sh`

## Usage
- A webcam preview window opens.
- Hold a card up to the camera and press **SPACE** to scan.
- The card name, year, and grade appear in the terminal and are logged to your sheet.
- Press **Q** to quit.
