# !pip install -q pykrx gspread oauth2client openpyxl
import os
import io
import sys
from datetime import datetime
import pandas as pd
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pykrx.stock import get_market_ohlcv_by_ticker

# --- Global Configuration & Constants ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# GitHub Actions will inject GOOGLE_APPLICATION_CREDENTIALS environment variable
# pointing to a service_account_key.json file generated on the fly
JSON_KEY_FILE_PATH = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'service_account_key.json')
GOOGLE_SHEET_NAME = 'KOSPI_Stock_Data'
WORKSHEET_NAME = 'Daily Data'

# --- Helper Functions ---

def parse_korean_number(s):
    if not isinstance(s, str):
        return s
    s = s.replace(',', '')
    if '백만' in s:
        s = s.replace('백만', '')
        try:
            return float(s) * 1_000_000
        except ValueError:
            return float(s) * 1_000_000_000_000
    if '조' in s or '억' in s:
        value = 0.0
        parts = s.split(' ')
        for part in parts:
            if '조' in part:
                value += float(part.replace('조', '')) * 1_000_000_000_000
            elif '억' in part:
                value += float(part.replace('억', '')) * 100_000_000
        return value
    try:
        return float(s)
    except ValueError:
        return s

def _fetch_api_data(url, section_name):
    print(f"{section_name} 수집 중...")
    try:
        res = requests.get(url, headers=HEADERS)
        if res.status_code == 200:
            print(f"✅ {section_name} 수집 완료")
            return res.json()
        print(f"❌ {section_name} 요청 실패: {res.status_code}")
    except Exception as e:
        print(f"❌ {section_name} 요청 중 에러 발생: {e}")
    return None

def _process_total_info(data_total):
    if data_total and 'totalInfos' in data_total:
        total_info_dict = {}
        if 'itemCode' in data_total:
            total_info_dict['종목코드'] = data_total['itemCode']
        if 'stockName' in data_total:
            total_info_dict['종목명'] = data_total['stockName']
        for item in data_total['totalInfos']:
            if 'key' in item and 'value' in item:
                total_info_dict[item['key']] = item['value']
        df = pd.DataFrame([total_info_dict])
        cols = df.columns.tolist()
        desired_order = []
        if '종목코드' in cols:
            desired_order.append('종목코드')
            cols.remove('종목코드')
        if '종목명' in cols:
            desired_order.append('종목명')
            cols.remove('종목명')
        return df[desired_order + cols]
    return pd.DataFrame()

def _process_finance_info(data_finance):
    if data_finance and 'financeInfo' in data_finance:
        finance_list = data_finance['financeInfo']
        if finance_list and 'rowList' in finance_list:
            flat_finance_data = {}
            for metric_item in finance_list['rowList']:
                metric_name = metric_item['title']
                for year, data_dict in metric_item.get('columns', {}).items():
                    column_name = f"{metric_name}_{year}"
                    flat_finance_data[column_name] = data_dict.get('value')
            return pd.DataFrame([flat_finance_data])
    return pd.DataFrame()

def get_kospi_stock_codes():
    today = datetime.now().strftime('%Y%m%d')
    print(f"Fetching KOSPI stock codes for {today} using pykrx...")
    try:
        df_pykrx = get_market_ohlcv_by_ticker(today, market='KOSPI')
        if df_pykrx.empty:
            print("No KOSPI data for today. Trying yesterday...")
            yesterday = (datetime.now() - pd.DateOffset(days=1)).strftime('%Y%m%d')
            df_pykrx = get_market_ohlcv_by_ticker(yesterday, market='KOSPI')
        if not df_pykrx.empty:
            codes = [str(code).zfill(6) for code in df_pykrx.index.tolist()]
            print(f"✅ Successfully retrieved {len(codes)} KOSPI stock codes.")
            return codes
        else:
            print("❌ Failed to retrieve KOSPI stock codes.")
    except Exception as e:
        print(f"❌ Error fetching KOSPI codes: {e}")
    return []

def collect_stock_data(symbols):
    all_stocks_data = []
    print(f"
Collecting data for {len(symbols)} stocks...")
    for i, symbol in enumerate(symbols):
        print(f"
Processing {symbol} ({i+1}/{len(symbols)})...")
        url_total = f"https://m.stock.naver.com/api/stock/{symbol}/integration"
        url_finance = f"https://m.stock.naver.com/api/stock/{symbol}/finance/annual"
        df_total = _process_total_info(_fetch_api_data(url_total, f"{symbol} 종합 정보"))
        data_finance = _fetch_api_data(url_finance, f"{symbol} 연간 재무 정보")
        df_finance = _process_finance_info(data_finance)
        df_combined = pd.DataFrame()
        if not df_total.empty and not df_finance.empty:
            df_combined = pd.concat([df_total, df_finance], axis=1)
        elif not df_total.empty:
            df_combined = df_total
        elif not df_finance.empty:
            if data_finance and data_finance.get('itemCode') and '종목코드' not in df_finance.columns:
                df_finance.insert(0, '종목코드', data_finance['itemCode'])
            df_combined = df_finance
        if not df_combined.empty:
            for col in ['대금', '시총']:
                if col in df_combined.columns:
                    df_combined[col] = df_combined[col].apply(parse_korean_number)
            all_stocks_data.append(df_combined)
        else:
            print(f"⚠️ Skipping {symbol} due to no combined data.")
    if all_stocks_data:
        return pd.concat(all_stocks_data, ignore_index=True)
    return pd.DataFrame()

def save_to_csv(df):
    if df.empty:
        return
    filename = f"kospi_stock_data_{datetime.now().strftime('%Y-%m-%d')}.csv"
    df.to_csv(filename, index=False, encoding='utf-8-sig')
    print(f"
✅ Successfully saved KOSPI stock data to {filename}")

def upload_to_google_sheets(df):
    if df.empty:
        return
    if not os.path.exists(JSON_KEY_FILE_PATH):
        print(f"⚠️ Skipping Google Sheets upload: Credentials file '{JSON_KEY_FILE_PATH}' not found.")
        return
    print("
Attempting to upload data to Google Sheets...")
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_KEY_FILE_PATH, scope)
        client = gspread.authorize(creds)
        print("✅ Successfully authenticated with Google Sheets.")
        try:
            spreadsheet = client.open(GOOGLE_SHEET_NAME)
        except gspread.exceptions.SpreadsheetNotFound:
            spreadsheet = client.create(GOOGLE_SHEET_NAME)
            print(f"✅ Created new Google Sheet: '{GOOGLE_SHEET_NAME}'.")
            print(f"⚠️ IMPORTANT: Please share this sheet with: {creds.client_email}")
        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows="1", cols="1")
        df_clean = df.fillna('')
        data_to_upload = [df_clean.columns.tolist()] + df_clean.values.tolist()
        worksheet.clear()
        worksheet.update(data_to_upload)
        worksheet.freeze(rows=1)
        print(f"✅ Successfully uploaded {len(df)} rows to Google Sheet '{GOOGLE_SHEET_NAME}'.")
    except Exception as e:
        print(f"❌ An error occurred during Google Sheets upload: {e}")

def main():
    kospi_symbols = get_kospi_stock_codes()
    if not kospi_symbols:
        print("No KOSPI stock codes found to process.")
        return
    final_df = collect_stock_data(kospi_symbols)
    if not final_df.empty:
        print("
" + "="*50)
        print("Final aggregated DataFrame for KOSPI stocks:")
        print(final_df.head())
        print(f"
Collected data for {len(final_df)} stocks ({final_df.shape[0]} rows, {final_df.shape[1]} columns).")
        save_to_csv(final_df)
        upload_to_google_sheets(final_df)
    else:
        print("
No data collected for any KOSPI stock.")

if __name__ == "__main__":
    main()
