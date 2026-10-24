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
# ⚙️ 구글 드라이브 & 시트 실제 ID 검증 완료 목록
# ==========================================
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

# 검증 완료된 실제 구글 드라이브 폴더 고유 ID
B2C_FOLDER_ID = "1ArGfyeVpZDJYUrdlGNSCrhj734JrqGW9"
B2B_FOLDER_ID = "1wpqrIBC8HnWTU20rShcg0Yvkcc1VIsml"
INBOUND_FOLDER_ID = "1BzKHxqaUrTFDubvJ7wnfXZqzNHaEvJjp"
DB_FOLDER_ID = "1jfi8ls7PWm9BWQUZwVYj9km5zAEuUKg9"         # [DB전용] 절대 삭제 금지
PROCESSED_FOLDER_ID = "1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ"  # 처리완료

DB_INBOUND_PATH = "wms_inbound.db"

# 검증 완료된 구글 시트 ID (대소문자 일치)
SHEET_ID_375 = "1x8zWC0BZRs4ji9N8-CLPb9RutbegxcVh0BXNAlHGS6g"
SHEET_ID_XFC = "1IGgpr8LezBs4hCySj_W34HSyHFhN6lt13uYSt3gbmqbs"

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
            fields="files(id, name, mimeType, parents)",
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
    try:
        file_metadata = {
            'name': subfolder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = service.files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
        return folder.get('id')
    except Exception as e:
        print(f"Subfolder create failed: {e}")
        return parent_id

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
    if pd.isna(val): return 0
    try:
        val_str = str(val).replace(',', '').strip()
        return int(float(val_str))
    except Exception:
        return 0

def parse_inbound_date(date_val):
    if pd.isna(date_val): return ""
    d_str = str(date_val).strip()
    match = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', d_str)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return ""

def update_inbound_plt_box_from_sheets(service, sheets_service, conn_ib):
    return 0, ""

# ==========================================
# 📊 구글 시트 M/H 데이터 동기화 모듈
# ==========================================
def parse_mh_375(sheets_service):
    """
    375 센터: [confidential] ES2_Operation Sheet (OB)
    시트탭: '[출고] 2026작업현황'
    날짜: A열, 시작시간: C열, M/H: AB열
    """
    rows_to_save = []
    try:
        range_name = "'[출고] 2026작업현황'!A5:AB10000"
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID_375, range=range_name
        ).execute()
        values = result.get('values', [])
        
        curr_date_str = ""
        curr_year = datetime.now().year

        for row in values:
            if not row: continue
            raw_date = str(row[0]).strip() if len(row) > 0 else ""
            if not raw_date and len(row) > 1:
                b_val = str(row[1]).strip()
                if "/" in b_val: raw_date = b_val

            if raw_date and "/" in raw_date:
                parts = raw_date.split()
                md = parts[0].split("/")
                if len(md) == 2 and md[0].isdigit() and md[1].isdigit():
                    try: curr_date_str = f"{curr_year}-{int(md[0]):02d}-{int(md[1]):02d}"
                    except Exception: pass
            
            start_time = str(row[2]).strip() if len(row) > 2 else ""
            mh_val_raw = str(row[27]).strip() if len(row) > 27 else ""
            
            if curr_date_str and ":" in start_time and mh_val_raw:
                clean_mh = mh_val_raw.replace(",", "").replace("-", "0")
                try:
                    mh_float = float(clean_mh)
                    h_int = int(start_time.split(":")[0])
                    time_label = f"{h_int:02d}:00 (D)" if h_int >= 6 else f"{h_int:02d}:00 (D+1)"
                    rows_to_save.append({
                        '영업마감일자': curr_date_str,
                        '시간대': time_label,
                        '센터그룹': '375',
                        'MH': mh_float
                    })
                except ValueError:
                    continue
    except Exception as e:
        print(f"375 MH 수집 에러: {e}")
    return rows_to_save

def parse_mh_xfc(sheets_service):
    """
    XFC 센터: (XFC센터)출고Dashboard
    시트탭: 'PPH-(일)(자동화)'
    날짜: A열, 시작시간: G열, 작업시간: I열, P&P: U열
    """
    rows_to_save = []
    try:
        range_name = "'PPH-(일)(자동화)'!A6:U10000"
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID_XFC, range=range_name
        ).execute()
        values = result.get('values', [])
        
        for row in values:
            if not row or len(row) < 21: continue
            raw_date = str(row[0]).strip()
            start_time = str(row[6]).strip()
            work_time_raw = str(row[8]).strip()
            pp_val_raw = str(row[20]).strip()
            
            if raw_date and ":" in start_time and work_time_raw and pp_val_raw:
                date_clean = raw_date[:10]
                try:
                    work_time = float(work_time_raw.replace(",", "").replace("-", "0"))
                    pp_val = float(pp_val_raw.replace(",", "").replace("-", "0"))
                    calculated_mh = round(work_time * pp_val, 2)
                    h_int = int(start_time.split(":")[0])
                    time_label = f"{h_int:02d}:00 (D)" if h_int >= 6 else f"{h_int:02d}:00 (D+1)"
                    rows_to_save.append({
                        '영업마감일자': date_clean,
                        '시간대': time_label,
                        '센터그룹': 'XFC',
                        'MH': calculated_mh
                    })
                except ValueError:
                    continue
    except Exception as e:
        print(f"XFC MH 수집 에러: {e}")
    return rows_to_save

def sync_mh_to_db(sheets_service, drive_service=None, db_path="wms_b2c.db"):
    list_375 = parse_mh_375(sheets_service)
    list_xfc = parse_mh_xfc(sheets_service)
    all_mh = list_375 + list_xfc
    
    if all_mh:
        df_mh = pd.DataFrame(all_mh)
        df_mh_agg = df_mh.groupby(['영업마감일자', '시간대', '센터그룹'])['MH'].sum().reset_index()
        target_dbs = [f for f in os.listdir('.') if f.startswith('wms_b2c') and f.endswith('.db')]
        if not target_dbs: target_dbs = [db_path]
            
        for d_path in set(target_dbs):
            try:
                conn = sqlite3.connect(d_path, timeout=10)
                df_mh_agg.to_sql("center_mh_hourly", conn, if_exists="replace", index=False)
                conn.close()
                if drive_service:
                    upload_db_to_drive(drive_service, d_path)
            except Exception as e:
                print(f"MH 테이블 DB 저장 에러 ({d_path}): {e}")
        return len(df_mh_agg)
    return 0

# ==========================================
# 🚀 메인 동기화 프로세스 실행 함수
# ==========================================
def process_and_update(service, sheets_service=None, progress_callback=None):
    matched_count = 0
    inbound_updated = False
    b2c_updated_months = set()
    b2c_errors = []
    processed_files_count = 0

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
        current_parents = f_info.get('parents', [B2C_FOLDER_ID])

        if progress_callback:
            progress_callback(idx, total_files, file_name, "")

        try:
            # 2-1. 엑셀 로드
            df_raw = read_excel_fast(service, file_id)
            if df_raw.empty:
                b2c_errors.append(f"{file_name}: 빈 파일")
                continue

            # 컬럼명 전처리 (공백 제거)
            df_raw.columns = [str(c).strip() for c in df_raw.columns]

            if '영업마감일자' not in df_raw.columns:
                b2c_errors.append(f"{file_name}: '영업마감일자' 컬럼 누락")
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

            # 2-2. SQLite 안전 적재
            conn_m = sqlite3.connect(month_db_name, timeout=15)
            cursor = conn_m.cursor()
            try:
                table_info = cursor.execute("PRAGMA table_info(shipment_raw)").fetchall()
                if table_info:
                    valid_cols = [c[1] for c in table_info]
                    common_cols = [c for c in df_b2c_valid.columns if c in valid_cols]
                    df_to_save = df_b2c_valid[common_cols]
                else:
                    df_to_save = df_b2c_valid
            except Exception:
                df_to_save = df_b2c_valid

            df_to_save.to_sql("shipment_raw", conn_m, if_exists="append", index=False)
            conn_m.commit()
            conn_m.close()

            b2c_updated_months.add(month_db_name)
            processed_files_count += 1

            # 2-3. 적재 완료 파일 처리완료 폴더로 이동
            target_month_str = latest_date_str[:7] if pd.notnull(latest_date_str) and len(str(latest_date_str)) >= 7 else datetime.now().strftime('%Y-%m')
            target_folder_id = get_or_create_subfolder(service, PROCESSED_FOLDER_ID, target_month_str)

            remove_parents_str = ",".join(current_parents)
            service.files().update(
                fileId=file_id,
                addParents=target_folder_id,
                removeParents=remove_parents_str,
                enforceSingleParent=True,
                supportsAllDrives=True,
                fields='id, parents'
            ).execute()

        except Exception as file_e:
            b2c_errors.append(f"{file_name}: {str(file_e)}")
            continue

    # 3. 입고 구글 시트 매칭 검수
    ib_err = ""
    try:
        matched_count, ib_err = update_inbound_plt_box_from_sheets(service, sheets_service, conn_ib)
    except Exception as sheet_e:
        ib_err = str(sheet_e)

    conn_ib.close()

    # 4. 갱신된 월별 DB 구글 드라이브 업로드
    for month_db_name in b2c_updated_months:
        upload_db_to_drive(service, month_db_name)

    if matched_count > 0 or inbound_updated:
        upload_db_to_drive(service, DB_INBOUND_PATH)

    # 5. 구글 시트 M/H 데이터 수집 및 DB 적재
    if sheets_service:
        try:
            sync_mh_to_db(sheets_service, drive_service=service)
        except Exception as mh_e:
            print(f"MH Sync Error: {mh_e}")

    # 최종 결과 결합 반환
    final_err_msg = ""
    if b2c_errors:
        final_err_msg = " | ".join(b2c_errors)
    if ib_err:
        final_err_msg = (final_err_msg + " | " + ib_err) if final_err_msg else ib_err

    # 성공한 파일 건수(없으면 매칭건수)와 에러 메시지 2개 반환
    result_count = processed_files_count if processed_files_count > 0 else matched_count
    return result_count, final_err_msg
