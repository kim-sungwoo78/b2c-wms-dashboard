import os
import io
import time
import sqlite3
import pandas as pd
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials

# 구글 드라이브 폴더 ID 설정
RAW_FOLDER_ID = "1Iqg4O8fS5E8eO2u0zKq04Jms3zJjM02F"       # B2C 대시보드 업로드 (신규 엑셀)
PROCESSED_FOLDER_ID = "15Ew-iXw65I2Z0f074RUp-H9wXw-E2B4m" # 처리완료 폴더

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

def sanitize_filename(name_str):
    return str(name_str).replace("/", "_").replace("\\", "_").replace(":", "_").replace("*", "_").replace("?", "_").replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_").strip()

def parse_clean_float(val):
    if not val:
        return 0.0
    try:
        clean_s = str(val).replace(",", "").strip()
        return float(clean_s)
    except Exception:
        return 0.0

def safe_list_files(service, query):
    try:
        results = service.files().list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora='allDrives'
        ).execute()
        return results.get('files', [])
    except Exception:
        try:
            results = service.files().list(
                q=query,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            return results.get('files', [])
        except Exception:
            return []

def get_or_create_dup_folder(service):
    try:
        q = f"'{PROCESSED_FOLDER_ID}' in parents and name = '[중복_확인필요]' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = safe_list_files(service, q)
        if res:
            return res[0]['id']
        
        folder_metadata = {
            'name': '[중복_확인필요]',
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [PROCESSED_FOLDER_ID]
        }
        folder = service.files().create(
            body=folder_metadata, 
            fields='id',
            supportsAllDrives=True
        ).execute()
        return folder.get('id')
    except Exception:
        return PROCESSED_FOLDER_ID

def read_excel_smart(fh, nrows=None):
    """엑셀 상단 제목/빈 행 위치에 유연하게 대응하는 헤더 스마트 자동 감지 로더"""
    for header_idx in [0, 1, 2, 3, 4]:
        try:
            fh.seek(0)
            df = pd.read_excel(fh, header=header_idx, nrows=nrows)
            cols_str = [str(c) for c in df.columns]
            if any('영업마감일자' in c or '마감일자' in c or '송장번호' in c for c in cols_str):
                return df
        except Exception:
            continue
    fh.seek(0)
    return pd.read_excel(fh, nrows=nrows)

def process_file_content(df, conn_b2c):
    """로우파일 엑셀을 읽어 마감일자 범위, 센터명 추출 및 daily_summary 축적"""
    # 컬럼명 정제
    col_map = {}
    for c in df.columns:
        sc = str(c).strip()
        if '영업마감일자' in sc or '마감일자' in sc:
            col_map[c] = '영업마감일자'
        elif sc == '센터' or '센터' in sc:
            col_map[c] = '센터'
        elif sc == '고객사' or '고객사' in sc:
            col_map[c] = '고객사'
        elif '배송속성' in sc:
            col_map[c] = '배송속성'
        elif '판매처' in sc:
            col_map[c] = '판매처'
        elif '출고박스' in sc or '박스종류' in sc:
            col_map[c] = '출고박스종류'
        elif 'SKU' in sc or '상품명' in sc:
            col_map[c] = 'SKU명'
        elif '바코드' in sc:
            col_map[c] = '바코드'
        elif '송장' in sc:
            col_map[c] = '송장번호'

    df_clean = df.rename(columns=col_map)

    # 마감일자 계산
    date_str = datetime.now().strftime('%Y%m%d')
    if '영업마감일자' in df_clean.columns and not df_clean['영업마감일자'].dropna().empty:
        clean_dates = df_clean['영업마감일자'].dropna().astype(str).str.replace("-", "").str.replace("/", "").str.strip()
        clean_dates = clean_dates[clean_dates.str.len() >= 8]
        if not clean_dates.empty:
            min_d = clean_dates.min()[:8]
            max_d = clean_dates.max()[:8]
            date_str = min_d if min_d == max_d else f"{min_d}_{max_d}"

    # 센터명 추출
    center_str = "통합센터"
    if '센터' in df_clean.columns and not df_clean['센터'].dropna().empty:
        center_str = sanitize_filename(df_clean['센터'].dropna().iloc[0])

    # daily_summary 집계 축적
    if '영업마감일자' in df_clean.columns and '센터' in df_clean.columns and '고객사' in df_clean.columns:
        group_cols = [c for c in ['영업마감일자', '센터', '고객사', '배송속성', '판매처', '출고박스종류', 'SKU명', '바코드'] if c in df_clean.columns]
        
        if '송장번호' in df_clean.columns:
            summary_df = df_clean.groupby(group_cols, dropna=False).agg(
                출고건수=('송장번호', 'nunique'),
                총출고수량=('송장번호', 'count')
            ).reset_index()
        else:
            summary_df = df_clean.groupby(group_cols, dropna=False).size().reset_index(name='출고건수')
            summary_df['총출고수량'] = summary_df['출고건수']

        # 날짜 포맷 통일 (YYYY-MM-DD)
        summary_df['영업마감일자'] = summary_df['영업마감일자'].astype(str).str.slice(0, 10)
        summary_df.to_sql('daily_summary', conn_b2c, if_exists='append', index=False)

    return date_str, center_str

def process_and_update(service, sheets_service=None, progress_callback=None):
    # DB 초기화 및 테이블 구조 확보
    conn_b2c = sqlite3.connect(DB_B2C_PATH, timeout=10)
    conn_b2c.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER
    )
    """)
    conn_b2c.commit()

    dup_folder_id = get_or_create_dup_folder(service)

    # 1. '처리완료' 폴더 및 '업로드' 폴더 전체 로우파일 스캔
    raw_files = safe_list_files(service, f"'{RAW_FOLDER_ID}' in parents and trashed = false and name contains '.xlsx'")
    processed_files = safe_list_files(service, f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '.xlsx'")

    all_target_files = []
    for f in processed_files:
        if f['name'] != "[중복_확인필요]" and not f['name'].startswith("[중복]"):
            all_target_files.append((f, PROCESSED_FOLDER_ID))
    for f in raw_files:
        all_target_files.append((f, RAW_FOLDER_ID))

    total_files = len(all_target_files)
    processed_cnt = 0
    dup_cnt = 0
    matched_inbound_count = 0
    err_msg = None

    # 2. 전수 로우파일 가공 및 파일명 정돈 / 초경량 DB 재집계
    for idx, (f_info, parent_folder) in enumerate(all_target_files, 1):
        file_id = f_info['id']
        orig_name = f_info['name']

        if progress_callback:
            progress_callback(idx, total_files, orig_name, "")

        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        try:
            df = read_excel_smart(fh)
            date_str, center_str = process_file_content(df, conn_b2c)

            # 새 파일명 생성
            seq_num = 1
            new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

            existing_q = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '{date_str}_{center_str}'"
            existing = safe_list_files(service, existing_q)
            if existing:
                seq_num = len(existing) + 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

            # 업로드 폴더에 있던 파일은 처리완료로 이동하면서 이름 변경
            if parent_folder == RAW_FOLDER_ID:
                service.files().update(
                    fileId=file_id,
                    addParents=PROCESSED_FOLDER_ID,
                    removeParents=RAW_FOLDER_ID,
                    body={'name': new_filename},
                    supportsAllDrives=True
                ).execute()
            else:
                # 이미 처리완료 폴더에 있던 파일은 이름만 변경
                if orig_name != new_filename:
                    service.files().update(
                        fileId=file_id,
                        body={'name': new_filename},
                        supportsAllDrives=True
                    ).execute()

            processed_cnt += 1
        except Exception as e:
            print(f"File error ({orig_name}): {e}")

    # 3. 구글 시트 입고 매칭
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
        except Exception as e:
            err_msg = str(e)

    # DB 압축 정리 (최종 초경량 15MB 유지)
    try:
        conn_b2c.execute("DELETE FROM shipment_raw WHERE 1=1;")
        conn_b2c.execute("VACUUM;")
        conn_b2c.commit()
    except Exception:
        pass

    conn_b2c.close()
    return matched_inbound_count, err_msg
