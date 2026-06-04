from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

creds = Credentials.from_service_account_file(
    '/Users/madden/Projects/card-scanner/google_creds.json',
    scopes=['https://www.googleapis.com/auth/spreadsheets']
)
svc = build('sheets', 'v4', credentials=creds)
svc.spreadsheets().values().append(
    spreadsheetId='1EiomYJ5KfjTz5t2CxX9MvVWeQdt_Js00BtKJIIYhZI8',
    range='Cards!A:I',
    valueInputOption='RAW',
    body={'values': [['TEST', 'Test Player', '2024', 'PSA 10', 'Test Card', '', '', '', '']]}
).execute()
print('SUCCESS - check your sheet!')
