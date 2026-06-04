from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

creds = Credentials.from_service_account_file(
    '/Users/madden/Projects/card-scanner/google_creds.json',
    scopes=['https://www.googleapis.com/auth/spreadsheets']
)
svc = build('sheets', 'v4', credentials=creds)

# Clear everything
svc.spreadsheets().values().clear(
    spreadsheetId='1EiomYJ5KfjTz5t2CxX9MvVWeQdt_Js00BtKJIIYhZI8',
    range='Cards!A:Z',
).execute()

# Write correct headers in row 1
svc.spreadsheets().values().update(
    spreadsheetId='1EiomYJ5KfjTz5t2CxX9MvVWeQdt_Js00BtKJIIYhZI8',
    range='Cards!A1',
    valueInputOption='RAW',
    body={'values': [['Value', 'Name', 'Year', 'Grade', 'Cert Number', 'Card', '', '', 'Paid', 'Tracking Number']]}
).execute()

print('Sheet fixed! Ready to scan.')
