import os
import io
import time
import sqlite3
import pandas as pd
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.service_account import Credentials

# 구글 드라이브 폴더 ID 설정
RAW_FOLDER_ID = "1Iqg4O8fS5E8eO2u0zKq04Jms3zJjM02F"       # 대시보드 업로드 폴더 ID
PROCESSED_FOLDER_ID = "15Ew-iXw65I2Z0f074RUp-H9wXw-E2B4m" # DB전용 & 처리완료 폴더 ID

# DB 파일 경로
DB_B2C_PATH = "wms_b2c.db"
DB_INBOUND_PATH = "wms_inbound.db"

def get_drive_service(creds_dict):
    scopes = ['https://www.googleapis.com/auth/drive']
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return build('drive', 'v3', credentials=credentials)

def get_sheets_service(creds_dict):
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return build('sheets', 'v4', credentials=credentials)

def parse_clean_float(val):
    if not val:
        return 0.0
    try:
        clean_s = str(val).replace(",", "").strip()
        return float(clean_s)
    except Exception:
        return 0.0

def safe_drive_list(service, query):
    """공유 드라이브/내 드라이브 호환 안전 목록 조회"""
    try:
        res = service.files().list(
            q=query,
            fields="files(id, name, parents, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return res.get('files', [])
    except Exception:
        try:
            res = service.files().list(q=query, fields="files(id, name, parents, mimeType)").execute()
            return res.get('files', [])
        except Exception:
            return []

def get_all_raw_excel_files(service):
    """B2C 로우 파일 및 부모 폴더 ID 함께 수집"""
    target_files = []
    queries = [
        f"'{RAW_FOLDER_ID}' in parents and trashed = false",
        "trashed = false and (name contains '송장' or name contains '출고' or name contains '.xlsx' or name contains '.xls')"
    ]
    seen_ids = set()
    for q in queries:
        items = safe_drive_list(service, q)
        for item in items:
            fid = item['id']
            fname = item['name']
            mtype = item.get('mimeType', '')
            if mtype != 'application/vnd.google-apps.folder':
                fname_l = fname.lower()
                if (fname_l.endswith('.xlsx') or fname_l.endswith('.xls')) and not fname_l.startswith('~$') and not fname_l.startswith('[중복]'):
                    if fid not in seen_ids:
                        seen_ids.add(fid)
                        parents = item.get('parents', [])
                        parent_id = parents[0] if parents else RAW_FOLDER_ID
                        if parent_id != PROCESSED_FOLDER_ID:
                            target_files.append((item, parent_id))
    return target_files

def process_and_update(service, sheets_service=None, progress_callback=None):
    # 1. DB 테이블 초기화
    conn_b2c = sqlite3.connect(DB_B2C_PATH, timeout=10)
    conn_b2c.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER
    )
    """)
    conn_b2c.execute("""
    CREATE TABLE IF NOT EXISTS shipment_raw (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER
    )
    """)
    conn_b2c.commit()

    # 2. B2C 로우 파일 수집
    target_files = get_all_raw_excel_files(service)
    total_files = len(target_files)
    processed_cnt = 0
    matched_inbound_count = 0

    if total_files == 0:
        conn_b2c.close()
        return 0, "ℹ️ 동기화할 B2C 로우 파일이 없습니다."

    # 3. 로우 파일 순회 가공 ➔ DB 저장 ➔ '처리완료' 폴더로 이동
    for idx, (f_info, actual_parent_id) in enumerate(target_files, 1):
        file_id = f_info['id']
        orig_name = f_info['name']

        if progress_callback:
            progress_callback(idx, total_files, orig_name, f"{idx}/{total_files} 파일 동기화 중")

        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        try:
            # 원본 헤더 자동 파싱
            df = None
            for h_idx in [0, 1, 2, 3]:
                try:
                    fh.seek(0)
                    df_temp = pd.read_excel(fh, header=h_idx)
                    cols_str = [str(c) for c in df_temp.columns]
                    if any('일자' in c or '마감' in c or '송장' in c for c in cols_str):
                        df = df_temp
                        break
                except Exception:
                    continue
            if df is None:
                fh.seek(0)
                df = pd.read_excel(fh)

            # 중복 컬럼 제거
            df = df.loc[:, ~df.columns.duplicated()]

            # 컬럼 매칭
            col_map = {}
            for c in df.columns:
                sc = str(c).strip()
                if any(k in sc for k in ['영업마감일자', '마감일자', '마감일', '출고일자', '일자']):
                    if '영업마감일자' not in col_map.values(): col_map[c] = '영업마감일자'
                elif any(k in sc for k in ['센터명', '센터', '출고지']):
                    if '센터' not in col_map.values(): col_map[c] = '센터'
                elif any(k in sc for k in ['고객사명', '고객사', '화주사']):
                    if '고객사' not in col_map.values(): col_map[c] = '고객사'
                elif '배송속성' in sc or '배송구분' in sc:
                    if '배송속성' not in col_map.values(): col_map[c] = '배송속성'
                elif '판매처' in sc or '채널' in sc:
                    if '판매처' not in col_map.values(): col_map[c] = '판매처'
                elif '출고박스' in sc or '박스' in sc:
                    if '출고박스종류' not in col_map.values(): col_map[c] = '출고박스종류'
                elif 'SKU' in sc or '상품명' in sc or '품목명' in sc:
                    if 'SKU명' not in col_map.values(): col_map[c] = 'SKU명'
                elif '바코드' in sc:
                    if '바코드' not in col_map.values(): col_map[c] = '바코드'
                elif '송장' in sc or '운송장' in sc:
                    if '송장번호' not in col_map.values(): col_map[c] = '송장번호'

            df_clean = df.rename(columns=col_map)
            df_clean = df_clean.loc[:, ~df_clean.columns.duplicated()]

            if '영업마감일자' not in df_clean.columns: df_clean['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')
            if '센터' not in df_clean.columns: df_clean['센터'] = '통합센터'
            if '고객사' not in df_clean.columns: df_clean['고객사'] = '기타'

            # 집계
            group_cols = [c for c in ['영업마감일자', '센터', '고객사', '배송속성', '판매처', '출고박스종류', 'SKU명', '바코드'] if c in df_clean.columns]
            if '송장번호' in df_clean.columns:
                summary_df = df_clean.groupby(group_cols, dropna=False).agg(
                    출고건수=('송장번호', 'nunique'),
                    총출고수량=('송장번호', 'count')
                ).reset_index()
            else:
                summary_df = df_clean.groupby(group_cols, dropna=False).size().reset_index(name='출고건수')
                summary_df['총출고수량'] = summary_df['출고건수']

            summary_df['영업마감일자'] = summary_df['영업마감일자'].astype(str).str.slice(0, 10)

            # DB 축적
            summary_df.to_sql('daily_summary', conn_b2c, if_exists='append', index=False)
            summary_df.to_sql('shipment_raw', conn_b2c, if_exists='append', index=False)

            # 가공 완료된 파일 '처리완료' 폴더로 이동
            if actual_parent_id != PROCESSED_FOLDER_ID:
                try:
                    service.files().update(
                        fileId=file_id,
                        addParents=PROCESSED_FOLDER_ID,
                        removeParents=actual_parent_id,
                        supportsAllDrives=True
                    ).execute()
                except Exception as mv_e:
                    print(f"File move warning ({orig_name}): {mv_e}")

            processed_cnt += 1

        except Exception as e:
            print(f"File process error ({orig_name}): {e}")

    conn_b2c.commit()
    conn_b2c.close()

    # 4. 입고 구글 시트 연동
    if sheets_service:
        try:
            conn_ib = sqlite3.connect(DB_INBOUND_PATH, timeout=10)
            SPREADSHEET_ID = "1j3yHXjpOpdYRBI_dFP6TBBG3Q3_vgbMAi3DW4po0SD0"
            range_name = "입고!A2:Z1000"
            result = sheets_service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=range_name).execute()
            rows = result.get('values', [])

            for row in rows:
                if len(row) >= 6:
                    ib_no = row[1] if len(row) > 1 else ""
                    plt_val = parse_clean_float(row[4]) if len(row) > 4 else 0.0
                    box_val = parse_clean_float(row[5]) if len(row) > 5 else 0.0

                    if ib_no:
                        conn_ib.execute("""
                            UPDATE inbound_summary
                            SET PLT수 = ?, BOX수 = ?
                            WHERE 입고번호 = ?
                        """, (plt_val, box_val, ib_no))
                        matched_inbound_count += 1

            conn_ib.commit()
            conn_ib.close()
        except Exception as ex:
            print(f"Inbound sheet sync error: {ex}")

    # 5. [DB전용] 절대 삭제 금지 폴더로 wms_b2c.db 자동 업로드
    try:
        db_q = f"'{PROCESSED_FOLDER_ID}' in parents and name = 'wms_b2c.db' and trashed = false"
        db_files = safe_drive_list(service, db_q)
        
        media = MediaFileUpload(DB_B2C_PATH, mimetype='application/x-sqlite3', resumable=True)
        if db_files:
            service.files().update(fileId=db_files[0]['id'], media_body=media, supportsAllDrives=True).execute()
        else:
            file_metadata = {'name': 'wms_b2c.db', 'parents': [PROCESSED_FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media, supportsAllDrives=True).execute()
    except Exception as db_err:
        print(f"DB Upload Warning: {db_err}")

    return matched_inbound_count, f"🎉 B2C 로우 파일 총 {processed_cnt}개 DB 반영, 드라이브 업로드 및 이동 완료!"
