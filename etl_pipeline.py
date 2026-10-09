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
RAW_FOLDER_ID = "1Iqg4O8fS5E8eO2u0zKq04Jms3zJjM02F"       # 대시보드 업로드
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
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])
    except Exception as e:
        print(f"Drive API List Error: {e}")
        return []

def get_all_excel_files(service):
    """'대시보드 업로드' 폴더 및 'B2C' 등 모든 하위 폴더의 엑셀 파일 전수 수집"""
    found_files = []
    
    # 1. 하위 폴더 검색 (B2C, B2B, 입고 등)
    subfolders = safe_list_files(service, f"'{RAW_FOLDER_ID}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    
    target_folder_ids = [RAW_FOLDER_ID]
    for sf in subfolders:
        if sf['name'] not in ['처리완료', '[DB전용] 절대 삭제 금지', '[중복_확인필요]']:
            target_folder_ids.append(sf['id'])

    # 2. 업로드 폴더들 내 .xlsx / .xls 파일 탐색
    for f_id in target_folder_ids:
        q = f"'{f_id}' in parents and trashed = false"
        files = safe_list_files(service, q)
        for f in files:
            fname = f['name'].lower()
            if (fname.endswith('.xlsx') or fname.endswith('.xls')) and not fname.startswith('~$'):
                found_files.append((f, f_id))
                
    return found_files

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

def read_excel_smart(fh):
    """엑셀 상단 제목/빈 행에 관계없이 데이터 헤더를 최적 자동 파싱"""
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
    """다양한 형태의 컬럼명을 유연하게 매칭하여 daily_summary 에 집계 저장"""
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

    # 기본값 보장
    if '영업마감일자' not in df_clean.columns:
        df_clean['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')
    if '센터' not in df_clean.columns:
        df_clean['센터'] = '통합센터'
    if '고객사' not in df_clean.columns:
        df_clean['고객사'] = '기타'

    # 날짜 범위 및 센터명 계산 (파일명 변경용)
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

    # daily_summary 집계 축적
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
    summary_df.to_sql('daily_summary', conn_b2c, if_exists='append', index=False)

    return date_str, center_str

def process_and_update(service, sheets_service=None, progress_callback=None):
    # DB 초기화 및 생성
    if os.path.exists(DB_B2C_PATH):
        try:
            os.remove(DB_B2C_PATH)
        except Exception:
            pass

    conn_b2c = sqlite3.connect(DB_B2C_PATH, timeout=10)
    conn_b2c.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER
    )
    """)
    conn_b2c.commit()

    dup_folder_id = get_or_create_dup_folder(service)

    # 1. 파일 전수 스캔 (업로드 영역 하위 B2C 폴더 포함 + 처리완료 영역)
    raw_files_with_parent = get_all_excel_files(service)
    processed_files = safe_list_files(service, f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false")

    all_target_files = []
    for f in processed_files:
        fname = f['name'].lower()
        if (fname.endswith('.xlsx') or fname.endswith('.xls')) and f['name'] != "[중복_확인필요]" and not f['name'].startswith("[중복]"):
            all_target_files.append((f, PROCESSED_FOLDER_ID))
            
    for f_item, parent_id in raw_files_with_parent:
        all_target_files.append((f_item, parent_id))

    total_files = len(all_target_files)
    processed_cnt = 0
    matched_inbound_count = 0
    err_msg = None

    # 2. 로우파일 수집 및 자동 집계
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

            seq_num = 1
            new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

            existing_q = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '{date_str}_{center_str}'"
            existing = safe_list_files(service, existing_q)
            if existing:
                seq_num = len(existing) + 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

            # 파일 이동 및 파일명 정돈
            if parent_folder != PROCESSED_FOLDER_ID:
                service.files().update(
                    fileId=file_id,
                    addParents=PROCESSED_FOLDER_ID,
                    removeParents=parent_folder,
                    body={'name': new_filename},
                    supportsAllDrives=True
                ).execute()
            else:
                if orig_name != new_filename:
                    service.files().update(
                        fileId=file_id,
                        body={'name': new_filename},
                        supportsAllDrives=True
                    ).execute()

            processed_cnt += 1
        except Exception as e:
            print(f"File process error ({orig_name}): {e}")

    # 3. 입고 구글 시트 매칭
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
