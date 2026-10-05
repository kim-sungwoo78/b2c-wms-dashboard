import os
import io
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

MAIN_UPLOAD_FOLDER_ID = '1UlsDUOZv3QPp19M_vMNptLiDZjEHPPUw'
PROCESSED_FOLDER_ID = '1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ'
DB_PATH = 'wms_dashboard.db'

def get_drive_service(creds_dict):
    creds = Credentials.from_service_account_info(
        creds_dict, 
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

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

def process_and_update(service):
    download_db_from_drive(service)

    conn = sqlite3.connect(DB_PATH)
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS raw_shipments (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, `배송 속성` TEXT,
        `판매 플랫폼` TEXT, `출고 박스` TEXT, SKU명 TEXT, 바코드 TEXT,
        `송장 번호` TEXT, `출고 수량` INTEGER
    )
    """)
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SKU명, 바코드)
    )
    """)

    query = f"'{MAIN_UPLOAD_FOLDER_ID}' in parents and trashed = false and name != '{DB_PATH}'"
    results = service.files().list(
        q=query, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute()
    files = results.get('files', [])

    new_files_processed = False

    for f in files:
        file_id, file_name = f['id'], f['name']
        
        if not (file_name.lower().endswith('.xlsx') or file_name.lower().endswith('.csv')):
            continue

        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        # CSV/Excel 분기 처리 (메모리 경량화)
        if file_name.lower().endswith('.csv'):
            df = pd.read_csv(fh)
        else:
            df = pd.read_excel(fh, engine='openpyxl')
        
        df.columns = [str(c).replace(" ", "").strip() for c in df.columns]

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
                df[tc] = None

        # 10,000건씩 분할하여 DB 저장 (메모리 안정화)
        chunk_size = 10000
        for i in range(0, len(df), chunk_size):
            chunk = df[target_cols].iloc[i:i+chunk_size]
            chunk.to_sql('raw_shipments', conn, if_exists='append', index=False)

        # 처리 완료 파일 이동
        service.files().update(
            fileId=file_id,
            addParents=PROCESSED_FOLDER_ID,
            removeParents=MAIN_UPLOAD_FOLDER_ID,
            supportsAllDrives=True,
            fields='id, parents'
        ).execute()

        new_files_processed = True

    if new_files_processed:
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
        GROUP BY 영업마감일자, 센터, 고객사, `배송 속성`, `판매 플랫폼`, 출고박스종류, SKU명, 바코드;
        """)

        cutoff_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        conn.execute("DELETE FROM raw_shipments WHERE 영업마감일자 < ?", (cutoff_date,))
        conn.commit()

    conn.close()

    if new_files_processed or not os.path.exists(DB_PATH):
        upload_db_to_drive(service)
