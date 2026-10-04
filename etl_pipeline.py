import os
import io
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

MAIN_UPLOAD_FOLDER_ID = '1UlsDUOZv3QPp19M_vMNptLiDZjEHPPUw'
PROCESSED_FOLDER_ID = '1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ'
DB_PATH = 'wms_dashboard.db'

def get_drive_service(creds_dict):
    creds = Credentials.from_service_account_info(
        creds_dict, 
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def process_and_update(service):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SKU명, 바코드)
    )
    """)

    query = f"'{MAIN_UPLOAD_FOLDER_ID}' in parents and (name contains '.xlsx' or name contains '.csv') and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

    for f in files:
        file_id, file_name = f['id'], f['name']
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        df = pd.read_excel(fh) if file_name.endswith('.xlsx') else pd.read_csv(fh)
        df.columns = [str(c).strip() for c in df.columns]

        if '마감 일시' in df.columns:
            df['마감일시_dt'] = pd.to_datetime(df['마감 일시'], errors='coerce')
            df['영업마감일자'] = (df['마감일시_dt'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
        else:
            df['영업마감일자'] = ""

        df.to_sql('raw_shipments', conn, if_exists='append', index=False)

        service.files().update(
            fileId=file_id,
            addParents=PROCESSED_FOLDER_ID,
            removeParents=MAIN_UPLOAD_FOLDER_ID,
            fields='id, parents'
        ).execute()

    conn.execute("""
    INSERT OR REPLACE INTO daily_summary
    SELECT 
        영업마감일자, 센터, 고객사,
        `배송 속성` AS 배송속성,
        `판매 플랫폼` AS 판매처,
        COALESCE(`출고 박스`, '미지정') AS 출고박스종류,
        SKU명, 바코드,
        COUNT(DISTINCT `송장 번호`) AS 출고건수,
        SUM(`출고 수량`) AS 총출고수량
    FROM raw_shipments
    WHERE 영업마감일자 IS NOT NULL AND 영업마감일자 != ''
    GROUP BY 영업마감일자, 센터, 고객사, `배송 속성`, `판매 플랫폼`, 출고박스종류, SKU명, 바코드;
    """)

    cutoff_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    conn.execute("DELETE FROM raw_shipments WHERE 영업마감일자 < ?", (cutoff_date,))
    conn.commit()
    conn.close()
