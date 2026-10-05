import os
import io
import time
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

MAIN_UPLOAD_FOLDER_ID = '1UlsDUOZv3QPp19M_vMNptLiDZjEHPPUw'
PROCESSED_FOLDER_ID = '1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ'
DB_PATH = 'wms_dashboard.db'

# 이천375 1층 구글 시트 ID
IB_SHEET_ID = '1j3yHXjpOpdYRBI_dFP6TBBG3Q3_vgbMAi3DW4po0SD0'

def get_drive_service(creds_dict):
    creds = Credentials.from_service_account_info(
        creds_dict, 
        scopes=[
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/spreadsheets.readonly'
        ]
    )
    return build('drive', 'v3', credentials=creds)

def get_sheets_service(creds_dict):
    creds = Credentials.from_service_account_info(
        creds_dict, 
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    return build('sheets', '4', credentials=creds)

def download_db_from_drive(service):
    query = f"'{MAIN_UPLOAD_FOLDER_ID}' in parents and name = '{DB_PATH}' and trashed = false"
    results = service.files().list(
        q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute()
    files = results.get('files', [])

    if files:
        file_id = files[0]['id']
        request = service.files().get_media(fileId=file_id)
        with open(DB_PATH, 'wb') as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    return False

def upload_db_to_drive(service):
    if not os.path.exists(DB_PATH):
        return
    query = f"'{MAIN_UPLOAD_FOLDER_ID}' in parents and name = '{DB_PATH}' and trashed = false"
    results = service.files().list(
        q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute()
    files = results.get('files', [])

    media = MediaFileUpload(DB_PATH, mimetype='application/x-sqlite3', resumable=True)

    if files:
        file_id = files[0]['id']
        service.files().update(
            fileId=file_id, media_body=media, supportsAllDrives=True
        ).execute()
    else:
        file_metadata = {
            'name': DB_PATH,
            'parents': [MAIN_UPLOAD_FOLDER_ID]
        }
        service.files().create(
            body=file_metadata, media_body=media, supportsAllDrives=True
        ).execute()

# 구글 시트 IB 정산 데이터 가져오기
def fetch_google_sheets_ib(sheets_service):
    try:
        sheet = sheets_service.spreadsheets()
        result = sheet.values().get(spreadsheetId=IB_SHEET_ID, range='IB!A1:Z2000').execute()
        values = result.get('values', [])
        if not values:
            return pd.DataFrame()
        
        headers = [str(h).strip() for h in values[0]]
        data = values[1:]
        
        # 열 크기 맞추기
        data_fixed = []
        for row in data:
            if len(row) < len(headers):
                row = row + [''] * (len(headers) - len(row))
            data_fixed.append(row[:len(headers)])
            
        df_sheet = pd.DataFrame(data_fixed, columns=headers)
        return df_sheet
    except Exception as e:
        print(f"Sheets Read Error: {e}")
        return pd.DataFrame()

def process_and_update(service, sheets_service=None, progress_callback=None):
    download_db_from_drive(service)

    conn = sqlite3.connect(DB_PATH)
    
    # B2C Raw 테이블
    conn.execute("""
    CREATE TABLE IF NOT EXISTS raw_shipments (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, `배송 속성` TEXT,
        `판매 플랫폼` TEXT, `출고 박스` TEXT, SKU명 TEXT, 바코드 TEXT,
        `송장 번호` TEXT, `출고 수량` INTEGER,
        PRIMARY KEY (영업마감일자, `송장 번호`, SKU명, 바코드)
    )
    """)
    
    # B2C 요약 테이블
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SKU명, 바코드)
    )
    """)

    # 입고 Raw 테이블
    conn.execute("""
    CREATE TABLE IF NOT EXISTS raw_inbound (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, `입고 번호` TEXT,
        SKU명 TEXT, 바코드 TEXT, `예정 수량` INTEGER, `총 검수 완료 수량` INTEGER, `입고 완료 일시` TEXT,
        PRIMARY KEY (영업마감일자, `입고 번호`, SKU명, 바코드)
    )
    """)

    # 입고 + 구글시트 통합 요약 테이블
    conn.execute("""
    CREATE TABLE IF NOT EXISTS inbound_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT,
        입고건수 INTEGER, 바코드수 INTEGER, 입고완료수량 INTEGER,
        PLT수 REAL, BOX수 REAL, 파적BOX수 REAL,
        PRIMARY KEY (영업마감일자, 센터, 고객사)
    )
    """)

    query = f"'{MAIN_UPLOAD_FOLDER_ID}' in parents and trashed = false and name != '{DB_PATH}'"
    results = service.files().list(
        q=query, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute()
    files = results.get('files', [])

    target_files = [f for f in files if f['name'].lower().endswith('.xlsx') or f['name'].lower().endswith('.csv')]
    total_count = len(target_files)

    new_files_processed = False
    start_time = time.time()

    for idx, f in enumerate(target_files, 1):
        file_id, file_name = f['id'], f['name']
        
        if progress_callback:
            elapsed = time.time() - start_time
            avg_time = elapsed / (idx - 1) if idx > 1 else 3.0
            rem_files = total_count - (idx - 1)
            eta_seconds = int(avg_time * rem_files)
            
            progress_callback(
                current=idx - 1, 
                total=total_count, 
                filename=file_name, 
                eta=eta_seconds
            )

        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        if file_name.lower().endswith('.csv'):
            df = pd.read_csv(fh)
        else:
            df = pd.read_excel(fh, engine='openpyxl')
        
        df.columns = [str(c).replace(" ", "").strip() for c in df.columns]

        # --- B2C 파일 vs 입고 파일 자동 판별 ---
        if '입고번호' in df.columns or '입고 번호' in df.columns or '총검수완료수량' in df.columns:
            # [입고 데이터 처리]
            date_col = '입고완료일시' if '입고완료일시' in df.columns else ('등록일시' if '등록일시' in df.columns else None)
            if not date_col:
                for c in df.columns:
                    if any(k in c for k in ['완료일시', '완료일자', '등록일시', '일자']):
                        date_col = c
                        break

            if date_col:
                df['dt_temp'] = pd.to_datetime(df[date_col], errors='coerce')
                df['영업마감일자'] = (df['dt_temp'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
            else:
                df['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')

            col_map_inbound = {
                '센터': '센터', '고객사': '고객사', '입고번호': '입고 번호',
                'SKU명': 'SKU명', '상품명': 'SKU명', '바코드': '바코드',
                '예정수량': '예정 수량', '총검수완료수량': '총 검수 완료 수량', '입고완료일시': '입고 완료 일시'
            }
            for k, v in col_map_inbound.items():
                if k in df.columns and v not in df.columns:
                    df[v] = df[k]

            target_in_cols = ['영업마감일자', '센터', '고객사', '입고 번호', 'SKU명', '바코드', '예정 수량', '총 검수 완료 수량', '입고 완료 일시']
            for tc in target_in_cols:
                if tc not in df.columns:
                    df[tc] = ''

            for fill_col in ['영업마감일자', '입고 번호', 'SKU명', '바코드']:
                df[fill_col] = df[fill_col].fillna('')

            insert_inbound_sql = """
            INSERT OR IGNORE INTO raw_inbound 
            (영업마감일자, 센터, 고객사, `입고 번호`, SKU명, 바코드, `예정 수량`, `총 검수 완료 수량`, `입고 완료 일시`)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.executemany(insert_inbound_sql, df[target_in_cols].to_numpy().tolist())

        else:
            # [B2C 출고 데이터 처리]
            date_col = None
            for c in df.columns:
                if any(k in c for k in ['마감일시', '마감일자', '출고일시', '출고일자', '일시', '일자']):
                    date_col = c
                    break

            if date_col:
                df['dt_temp'] = pd.to_datetime(df[date_col], errors='coerce')
                df['영업마감일자'] = (df['dt_temp'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
            else:
                df['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')

            col_map = {
                '센터': '센터', '고객사': '고객사', '배송속성': '배송 속성', '배송유형': '배송 속성',
                '판매플랫폼': '판매 플랫폼', '판매처': '판매 플랫폼', '출고박스': '출고 박스',
                '박스종류': '출고 박스', 'SKU명': 'SKU명', '상품명': 'SKU명',
                '바코드': '바코드', '송장번호': '송장 번호', '출고수량': '출고 수량', '수량': '출고 수량'
            }
            for k, v in col_map.items():
                if k in df.columns and v not in df.columns:
                    df[v] = df[k]

            target_cols = ['영업마감일자', '센터', '고객사', '배송 속성', '판매 플랫폼', '출고 박스', 'SKU명', '바코드', '송장 번호', '출고 수량']
            for tc in target_cols:
                if tc not in df.columns:
                    df[tc] = ''

            for fill_col in ['영업마감일자', 'SKU명', '바코드', '송장 번호']:
                df[fill_col] = df[fill_col].fillna('')

            insert_sql = """
            INSERT OR IGNORE INTO raw_shipments 
            (영업마감일자, 센터, 고객사, `배송 속성`, `판매 플랫폼`, `출고 박스`, SKU명, 바코드, `송장 번호`, `출고 수량`)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.executemany(insert_sql, df[target_cols].to_numpy().tolist())

        # 처리 완료 파일 이동
        service.files().update(
            fileId=file_id,
            addParents=PROCESSED_FOLDER_ID,
            removeParents=MAIN_UPLOAD_FOLDER_ID,
            supportsAllDrives=True,
            fields='id, parents'
        ).execute()

        new_files_processed = True

    if progress_callback and total_count > 0:
        progress_callback(current=total_count, total=total_count, filename="구글 시트 연동 및 통합 DB 집계 중...", eta=0)

    # --- 구글 시트 데이터 불러오기 및 입고 요약 DB 구축 ---
    df_sheet = pd.DataFrame()
    if sheets_service:
        df_sheet = fetch_google_sheets_ib(sheets_service)

    # 1. B2C 요약 데이터 재집계
    if new_files_processed:
        conn.execute("DELETE FROM daily_summary;")
        conn.execute("""
        INSERT OR REPLACE INTO daily_summary
        SELECT 
            영업마감일자,
            COALESCE(센터, '미지정') AS 센터,
            COALESCE(고객사, '미지정') AS 고객사,
            COALESCE(`배송 속성`, '미지정') AS 배송속성,
            COALESCE(`판매 플랫폼`, '미지정') AS 판매처,
            COALESCE(`출고 박스`, '미지정') AS 출고박스종류,
            COALESCE(SKU명, '미지정') AS SKU명,
            COALESCE(바코드, '미지정') AS 바코드,
            COUNT(DISTINCT `송장 번호`) AS 출고건수,
            SUM(CAST(COALESCE(`출고 수량`, 1) AS INTEGER)) AS 총출고수량
        FROM raw_shipments
        WHERE 영업마감일자 IS NOT NULL AND 영업마감일자 != ''
        GROUP BY 영업마감일자, 센터, 고객사, `배송 속성`, `판매처`, 출고박스종류, SKU명, 바코드;
        """)

    # 2. 입고 + 구글 시트(PLT/BOX) 매칭 요약 데이터 재집계
    df_raw_inbound = pd.read_sql("SELECT * FROM raw_inbound", conn)
    if not df_raw_inbound.empty:
        inbound_grp = df_raw_inbound.groupby(['영업마감일자', '센터', '고객사', '입고 번호']).agg(
            바코드수=('바코드', 'nunique'),
            입고완료수량=('총 검수 완료 수량', lambda x: pd.to_numeric(x, errors='coerce').sum())
        ).reset_index()

        # 구글 시트에서 PLT, BOX 정보 매칭 (작업번호 = 입고 번호)
        if not df_sheet.empty and '작업번호' in df_sheet.columns:
            df_sheet_sub = df_sheet[['작업번호', 'PLT', 'BOX', '파적 BOX']].copy()
            df_sheet_sub.columns = ['입고 번호', 'PLT수', 'BOX수', '파적BOX수']
            for col_c in ['PLT수', 'BOX수', '파적BOX수']:
                df_sheet_sub[col_c] = pd.to_numeric(df_sheet_sub[col_c].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

            inbound_grp = pd.merge(inbound_grp, df_sheet_sub, on='입고 번호', how='left').fillna(0)
        else:
            inbound_grp['PLT수'] = 0
            inbound_grp['BOX수'] = 0
            inbound_grp['파적BOX수'] = 0

        final_inbound_summary = inbound_grp.groupby(['영업마감일자', '센터', '고객사']).agg(
            입고건수=('입고 번호', 'nunique'),
            바코드수=('바코드수', 'sum'),
            입고완료수량=('입고완료수량', 'sum'),
            PLT수=('PLT수', 'sum'),
            BOX수=('BOX수', 'sum'),
            파적BOX수=('파적BOX수', 'sum')
        ).reset_index()

        conn.execute("DELETE FROM inbound_summary;")
        final_inbound_summary.to_sql('inbound_summary', conn, if_exists='append', index=False)

    conn.commit()
    conn.close()

    if new_files_processed or not os.path.exists(DB_PATH):
        upload_db_to_drive(service)
