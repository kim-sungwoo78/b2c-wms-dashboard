import os
import io
import re
import math
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ==========================================
# ⚙️ 구글 드라이브 & 시트 실제 ID 설정
# ==========================================
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

# 실제 구글 드라이브 폴더 고유 ID
B2C_FOLDER_ID = "1ArGfyeVpZDJYUrdlGNSCrhj734JrqGW9"
B2B_FOLDER_ID = "1wpqrIBC8HnWTU20rShcg0Yvkcc1VIsml"
INBOUND_FOLDER_ID = "1BzKHxqaUrTFDubvJ7wnfXZqzNHaEvJjp"
DB_FOLDER_ID = "1jfi8ls7PWm9BWQUZwVYj9km5zAEuUKg9"  # [DB전용] 절대 삭제 금지
PROCESSED_FOLDER_ID = "1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ"  # 처리완료

DB_INBOUND_PATH = "wms_inbound.db"

# PPH 구글 시트 실제 ID
SHEET_ID_375 = "1x8zWC0BZRs4ji9N8-CLPb9RutbegxcVh0BXNAlhGS6g"
SHEET_ID_XFC = "1Bgpr8LezBs4hCy5j_W34RSyHFIN68zT3uY5i3gbmqbs"

# ==========================================
# 🔑 구글 API 서비스 연결 함수
# ==========================================
def get_drive_service(creds_dict):
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)

def get_sheets_service(creds_dict):
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds)

def safe_drive_list(service, query):
    try:
        results = service.files().list(
            q=query,
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])
    except Exception as e:
        print(f"Drive list error: {e}")
        return []

def get_or_create_subfolder(service, parent_id, subfolder_name):
    query = f"'{parent_id}' in parents and name = '{subfolder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    items = safe_drive_list(service, query)
    if items:
        return items[0]['id']
    file_metadata = {
        'name': subfolder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = service.files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
    return folder.get('id')

def download_db_from_drive(service, db_filename):
    query = f"'{DB_FOLDER_ID}' in parents and name = '{db_filename}' and trashed = false"
    items = safe_drive_list(service, query)
    if items:
        file_id = items[0]['id']
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        fh = io.FileIO(db_filename, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.close()
        return True
    return False

def upload_db_to_drive(service, db_filename):
    if not os.path.exists(db_filename):
        return
    query = f"'{DB_FOLDER_ID}' in parents and name = '{db_filename}' and trashed = false"
    items = safe_drive_list(service, query)
    media = MediaFileUpload(db_filename, mimetype='application/x-sqlite3', resumable=True)
    if items:
        file_id = items[0]['id']
        service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
    else:
        file_metadata = {'name': db_filename, 'parents': [DB_FOLDER_ID]}
        service.files().create(body=file_metadata, media_body=media, fields='id', supportsAllDrives=True).execute()

def list_files_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    return safe_drive_list(service, query)

def read_excel_fast(service, file_id):
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return pd.read_excel(fh)

def parse_val(val):
    if pd.isna(val):
        return 0
    try:
        val_str = str(val).replace(',', '').strip()
        return int(float(val_str))
    except Exception:
        return 0

def parse_inbound_date(date_val):
    if pd.isna(date_val):
        return ""
    d_str = str(date_val).strip()
    match = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', d_str)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return ""

def update_inbound_plt_box_from_sheets(service, sheets_service, conn_ib):
    return 0, ""

# ==========================================
# 📊 구글 시트 PPH(시간당 처리량) 데이터 동기화 모듈
# ==========================================
def parse_pph_375(sheets_service):
    """
    375 센터: [confidential] ES2_Operation Sheet (OB)
    시트탭: '[출고] 2026작업현황'
    날짜: B열, 시작시간: C열, PPH: AC열
    """
    rows_to_save = []
    try:
        range_name = "'[출고] 2026작업현황'!B5:AC10000"
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID_375, range=range_name
        ).execute()
        values = result.get('values', [])
        
        curr_date_str = ""
        curr_year = datetime.now().year

        for row in values:
            if not row:
                continue
            
            raw_b = str(row[0]).strip() if len(row) > 0 else ""
            if raw_b and "/" in raw_b:
                parts = raw_b.split()
                md = parts[0].split("/")
                if len(md) == 2:
                    try:
                        curr_date_str = f"{curr_year}-{int(md[0]):02d}-{int(md[1]):02d}"
                    except Exception:
                        pass
            
            start_time = str(row[1]).strip() if len(row) > 1 else ""
            pph_val_raw = str(row[27]).strip() if len(row) > 27 else ""
            
            if curr_date_str and ":" in start_time and pph_val_raw:
                clean_pph = pph_val_raw.replace(",", "").replace("-", "0")
                try:
                    pph_float = float(clean_pph)
                    h_int = int(start_time.split(":")[0])
                    time_label = f"{h_int:02d}:00 (D)" if h_int >= 6 else f"{h_int:02d}:00 (D+1)"
                    
                    rows_to_save.append({
                        '영업마감일자': curr_date_str,
                        '시간대': time_label,
                        '센터': '이천375 1층(네이버)',
                        'PPH': pph_float
                    })
                except ValueError:
                    continue
    except Exception as e:
        print(f"375 PPH 수집 에러: {e}")
        
    return rows_to_save

def parse_pph_xfc(sheets_service):
    """
    XFC 센터: (XFC센터)출고Dashboard
    시트탭: 'PPH-(일)(자동화)'
    날짜: A열, 시작시간: G열, PPH: S열
    """
    rows_to_save = []
    try:
        range_name = "'PPH-(일)(자동화)'!A6:S10000"
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID_XFC, range=range_name
        ).execute()
        values = result.get('values', [])
        
        for row in values:
            if not row or len(row) < 19:
                continue
            
            raw_date = str(row[0]).strip()
            start_time = str(row[6]).strip()
            pph_val_raw = str(row[18]).strip()
            
            if raw_date and ":" in start_time and pph_val_raw:
                date_clean = raw_date[:10]
                clean_pph = pph_val_raw.replace(",", "").replace("-", "0")
                try:
                    pph_float = float(clean_pph)
                    h_int = int(start_time.split(":")[0])
                    time_label = f"{h_int:02d}:00 (D)" if h_int >= 6 else f"{h_int:02d}:00 (D+1)"
                    
                    rows_to_save.append({
                        '영업마감일자': date_clean,
                        '시간대': time_label,
                        '센터': '이천375 XFC(네이버)',
                        'PPH': pph_float
                    })
                except ValueError:
                    continue
    except Exception as e:
        print(f"XFC PPH 수집 에러: {e}")
        
    return rows_to_save

def sync_pph_to_db(sheets_service, drive_service=None, db_path="wms_b2c.db"):
    list_375 = parse_pph_375(sheets_service)
    list_xfc = parse_pph_xfc(sheets_service)
    all_pph = list_375 + list_xfc
    
    if all_pph:
        df_pph = pd.DataFrame(all_pph)
        target_dbs = [f for f in os.listdir('.') if f.startswith('wms_b2c') and f.endswith('.db')]
        if not target_dbs:
            target_dbs = [db_path]
            
        for d_path in set(target_dbs):
            try:
                conn = sqlite3.connect(d_path, timeout=10)
                df_pph.to_sql("center_pph_hourly", conn, if_exists="replace", index=False)
                conn.close()
                if drive_service:
                    upload_db_to_drive(drive_service, d_path)
            except Exception as e:
                print(f"PPH 테이블 DB 저장 에러 ({d_path}): {e}")
                
        return len(df_pph)
    return 0

# ==========================================
# 🚀 메인 동기화 프로세스 실행 함수
# ==========================================
def process_and_update(service, sheets_service=None, progress_callback=None):
    matched_count = 0
    err_msg = ""
    inbound_updated = False
    b2c_updated_months = set()

    # 1. 입고 DB 초기화 및 연결
    if not os.path.exists(DB_INBOUND_PATH):
        download_db_from_drive(service, DB_INBOUND_PATH)
    conn_ib = sqlite3.connect(DB_INBOUND_PATH, timeout=10)

    # 2. B2C 출고 신규 파일 검색 및 ETL
    b2c_files = list_files_in_folder(service, B2C_FOLDER_ID)
    total_files = len(b2c_files)

    for idx, f_info in enumerate(b2c_files, 1):
        file_id = f_info['id']
        file_name = f_info['name']

        if progress_callback:
            progress_callback(idx, total_files, file_name, "")

        try:
            df_raw = read_excel_fast(service, file_id)
            if df_raw.empty or '영업마감일자' not in df_raw.columns:
                continue

            df_b2c_valid = df_raw.dropna(subset=['영업마감일자']).copy()
            df_b2c_valid['영업마감일자'] = df_b2c_valid['영업마감일자'].astype(str).str[:10]

            latest_date_str = df_b2c_valid['영업마감일자'].max()
            if pd.notnull(latest_date_str) and len(str(latest_date_str)) >= 7:
                curr_ym = str(latest_date_str)[:7].replace('-', '')
            else:
                curr_ym = datetime.now().strftime('%Y%m')

            month_db_name = f"wms_b2c_{curr_ym}.db"
            if not os.path.exists(month_db_name):
                download_db_from_drive(service, month_db_name)

            conn_m = sqlite3.connect(month_db_name, timeout=10)
            df_b2c_valid.to_sql("shipment_raw", conn_m, if_exists="append", index=False)
            conn_m.commit()
            conn_m.close()

            b2c_updated_months.add(month_db_name)

            # 처리 완료 폴더 내 연월(YYYY-MM) 서브폴더로 파일 이동
            target_month_str = latest_date_str[:7] if pd.notnull(latest_date_str) and len(str(latest_date_str)) >= 7 else datetime.now().strftime('%Y-%m')
            target_folder_id = get_or_create_subfolder(service, PROCESSED_FOLDER_ID, target_month_str)

            service.files().update(
                fileId=file_id,
                addParents=target_folder_id,
                removeParents=B2C_FOLDER_ID,
                supportsAllDrives=True,
                fields='id, parents'
            ).execute()

        except Exception as file_e:
            print(f"File process error ({file_name}): {file_e}")
            continue

    # 3. 입고 구글 시트 매칭 검수
    try:
        matched_count, err_msg = update_inbound_plt_box_from_sheets(service, sheets_service, conn_ib)
    except Exception as sheet_e:
        err_msg = str(sheet_e)

    conn_ib.close()

    # 4. 갱신된 DB 구글 드라이브 업로드
    for month_db_name in b2c_updated_months:
        upload_db_to_drive(service, month_db_name)

    if matched_count > 0 or inbound_updated:
        upload_db_to_drive(service, DB_INBOUND_PATH)

    # 5. 구글 시트 시간대별 PPH 데이터 수집 및 DB 적재/백업
    if sheets_service:
        try:
            sync_pph_to_db(sheets_service, drive_service=service)
        except Exception as pph_e:
            print(f"PPH Sync Error: {pph_e}")

    return matched_count, err_msg
