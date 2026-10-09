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
RAW_FOLDER_ID = "1Iqg4O8fS5E8eO2u0zKq04Jms3zJjM02F"       # 대시보드 업로드 폴더 ID
PROCESSED_FOLDER_ID = "15Ew-iXw65I2Z0f074RUp-H9wXw-E2B4m" # 처리완료 폴더 ID

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

def safe_list_files(service, folder_id):
    """지정 폴더 내 파일 안전 목록 조회"""
    try:
        q = f"'{folder_id}' in parents and trashed = false"
        results = service.files().list(
            q=q,
            fields="files(id, name, parents, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get('files', [])
        
        valid_files = []
        for f in files:
            if f.get('mimeType') != 'application/vnd.google-apps.folder':
                if not f['name'].startswith('~$'):
                    valid_files.append(f)
        return valid_files
    except Exception as e:
        print(f"List files error: {e}")
        return []

def safe_list_subfolders(service, parent_id):
    """하위 폴더(B2C, B2B 등) 목록 조회"""
    try:
        q = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        results = service.files().list(
            q=q,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])
    except Exception:
        return []

def read_excel_smart(fh):
    """엑셀 상단 헤더 위치 자동 감지 로더"""
    for header_idx in [0, 1, 2, 3, 4, 5]:
        try:
            fh.seek(0)
            df = pd.read_excel(fh, header=header_idx)
            cols_str = [str(c) for c in df.columns]
            if any(k in c for c in cols_str for k in ['마감', '일자', '센터', '고객', '송장']):
                return df
        except Exception:
            continue
    fh.seek(0)
    return pd.read_excel(fh)

def process_file_content(df, conn_b2c):
    """유연한 컬럼 매칭 및 daily_summary / shipment_raw 집계 동시 저장"""
    col_map = {}
    for c in df.columns:
        sc = str(c).strip()
        if any(k in sc for k in ['영업마감일자', '마감일자', '마감일', '출고일자', '일자']):
            if '영업마감일자' not in col_map.values():
                col_map[c] = '영업마감일자'
        elif any(k in sc for k in ['센터명', '센터', '출고지']):
            if '센터' not in col_map.values():
                col_map[c] = '센터'
        elif any(k in sc for k in ['고객사명', '고객사', '화주사']):
            if '고객사' not in col_map.values():
                col_map[c] = '고객사'
        elif '배송속성' in sc or '배송구분' in sc:
            col_map[c] = '배송속성'
        elif '판매처' in sc or '채널' in sc:
            col_map[c] = '판매처'
        elif '출고박스' in sc or '박스' in sc:
            col_map[c] = '출고박스종류'
        elif 'SKU' in sc or '상품명' in sc or '품목명' in sc:
            col_map[c] = 'SKU명'
        elif '바코드' in sc:
            col_map[c] = '바코드'
        elif '송장' in sc or '운송장' in sc:
            col_map[c] = '송장번호'

    df_clean = df.rename(columns=col_map)

    # 기본값 설정
    if '영업마감일자' not in df_clean.columns:
        df_clean['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')
    if '센터' not in df_clean.columns:
        df_clean['센터'] = '통합센터'
    if '고객사' not in df_clean.columns:
        df_clean['고객사'] = '기타'

    # 날짜 범위 및 센터명 산출 (파일명 포맷용)
    clean_dates = df_clean['영업마감일자'].dropna().astype(str).str.replace("-", "").str.replace("/", "").str.strip()
    clean_dates = clean_dates[clean_dates.str.len() >= 8]
    if not clean_dates.empty:
        min_d = clean_dates.min()[:8]
        max_d = clean_dates.max()[:8]
        date_str = min_d if min_d == max_d else f"{min_d}_{max_d}"
    else:
        date_str = datetime.now().strftime('%Y%m%d')

    center_val = df_clean['센터'].dropna().iloc[0] if not df_clean['센터'].dropna().empty else "통합센터"
    center_str = sanitize_filename(center_val)

    # 요약 집계
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

    # ★ [핵심] daily_summary 와 shipment_raw 두 테이블에 모두 보존하여 대시보드 100% 호환 ★
    summary_df.to_sql('daily_summary', conn_b2c, if_exists='append', index=False)
    summary_df.to_sql('shipment_raw', conn_b2c, if_exists='append', index=False)

    return date_str, center_str

def process_and_update(service, sheets_service=None, progress_callback=None):
    # 1. DB 연결 및 테이블 생성
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

    # 2. 타겟 업로드 폴더 수집 ('대시보드 업로드' 및 'B2C' 등 하위 폴더)
    target_folders = [RAW_FOLDER_ID]
    subfolders = safe_list_subfolders(service, RAW_FOLDER_ID)
    for sf in subfolders:
        if sf['name'] not in ['처리완료', '[DB전용] 절대 삭제 금지', '[중복_확인필요]']:
            target_folders.append(sf['id'])

    # 업로드 대상 신규 로우파일 수집
    raw_files_to_process = []
    for folder_id in target_folders:
        files = safe_list_files(service, folder_id)
        for f in files:
            raw_files_to_process.append((f, folder_id))

    total_files = len(raw_files_to_process)
    processed_cnt = 0
    matched_inbound_count = 0
    err_msg = None

    # 3. 신규 로우파일 가공 및 파일명 자동 변경 / '처리완료' 이동
    for idx, (f_info, parent_folder_id) in enumerate(raw_files_to_process, 1):
        file_id = f_info['id']
        orig_name = f_info['name']

        if progress_callback:
            progress_callback(idx, total_files, orig_name, "")

        # 파일 다운로드
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

            # 새 파일명 생성 ([마감일]_[센터명]_[순번].xlsx)
            seq_num = 1
            new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"
            if not new_filename.endswith('.xlsx'):
                new_filename += '.xlsx'

            # '처리완료' 폴더 내 기존 파일 확인
            existing_files = safe_list_files(service, PROCESSED_FOLDER_ID)
            dup_matches = [ef for ef in existing_files if date_str in ef['name'] and center_str in ef['name']]
            if dup_matches:
                seq_num = len(dup_matches) + 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

            # 구글 드라이브 파일명 변경 및 '처리완료' 폴더로 이동
            current_parents = f_info.get('parents', [parent_folder_id])
            remove_parents_str = ",".join(current_parents)

            service.files().update(
                fileId=file_id,
                addParents=PROCESSED_FOLDER_ID,
                removeParents=remove_parents_str,
                body={'name': new_filename},
                supportsAllDrives=True
            ).execute()

            processed_cnt += 1
        except Exception as e:
            print(f"File process error ({orig_name}): {e}")

    # 4. 입고 구글 시트 매칭
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

    conn_b2c.close()
    return matched_inbound_count, err_msg
