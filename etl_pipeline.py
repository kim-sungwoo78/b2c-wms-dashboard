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
    """'22,000' 과 같은 콤마 포함 문자열 수치를 안전하게 float로 변환"""
    if not val:
        return 0.0
    try:
        clean_s = str(val).replace(",", "").strip()
        return float(clean_s)
    except Exception:
        return 0.0

def safe_list_files(service, query):
    """공유 드라이브(Shared Drive) 호환성을 보장하는 파일 안전 조회 함수"""
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
    """'처리완료' 폴더 내에 [중복_확인필요] 폴더가 없으면 자동 생성 후 ID 반환"""
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

def rename_existing_processed_files(service):
    """'처리완료' 폴더에 이미 보관 중인 예전 파일들의 이름을 [마감일자_센터명_순번]으로 일괄 변경"""
    try:
        q = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '.xlsx'"
        files = safe_list_files(service, q)

        for f in files:
            file_id = f['id']
            orig_name = f['name']

            # 이미 정돈된 패턴 및 특수 폴더는 건너뜀
            if orig_name.startswith("[중복]") or orig_name == "[중복_확인필요]":
                continue

            # 파일 다운로드 및 헤더 추출 (대형 파일 고려 nrows=50 설정으로 초속 읽기)
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)

            try:
                # 상위 50행만 읽어 날짜 및 센터명 빠르게 판별
                df = pd.read_excel(fh, nrows=50)
                
                date_str = "20261008"
                if '영업마감일자' in df.columns and not df['영업마감일자'].dropna().empty:
                    clean_dates = df['영업마감일자'].dropna().astype(str).str.replace("-", "").str.replace("/", "").str.strip()
                    min_d = clean_dates.min()[:8]
                    max_d = clean_dates.max()[:8]
                    
                    if min_d == max_d:
                        date_str = min_d
                    else:
                        date_str = f"{min_d}_{max_d}"

                center_str = "통합센터"
                if '센터' in df.columns and not df['센터'].dropna().empty:
                    center_str = sanitize_filename(df['센터'].dropna().iloc[0])

                seq_num = 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

                if orig_name == new_filename:
                    continue

                existing_q = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '{date_str}_{center_str}'"
                existing = safe_list_files(service, existing_q)
                
                if existing:
                    seq_num = len(existing) + 1
                    new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

                # 구글 드라이브 상의 파일명 즉시 업데이트
                service.files().update(
                    fileId=file_id, 
                    body={'name': new_filename},
                    supportsAllDrives=True
                ).execute()
            except Exception as ex:
                print(f"File rename exception for {orig_name}: {ex}")
    except Exception as e:
        print(f"Batch rename warning: {e}")

def process_and_update(service, sheets_service=None, progress_callback=None):
    # 0. 기존 '처리완료' 폴더 파일들 이름 일괄 정돈 실행
    rename_existing_processed_files(service)

    # 1. 신규 엑셀 파일 스캔
    query = f"'{RAW_FOLDER_ID}' in parents and trashed = false and name contains '.xlsx'"
    files = safe_list_files(service, query)

    total_files = len(files)
    processed_files_count = 0
    dup_files_count = 0
    matched_inbound_count = 0
    err_msg = None

    dup_folder_id = get_or_create_dup_folder(service)
    conn_b2c = sqlite3.connect(DB_B2C_PATH, timeout=10)
    
    # 2. 신규 로우파일 가공 및 파일명 변경 / 중복 체크 처리
    for idx, f in enumerate(files, 1):
        file_id = f['id']
        orig_name = f['name']

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
            df = pd.read_excel(fh)
            
            date_str = datetime.now().strftime('%Y%m%d')
            if '영업마감일자' in df.columns and not df['영업마감일자'].dropna().empty:
                clean_dates = df['영업마감일자'].dropna().astype(str).str.replace("-", "").str.replace("/", "").str.strip()
                min_d = clean_dates.min()[:8]
                max_d = clean_dates.max()[:8]
                
                if min_d == max_d:
                    date_str = min_d
                else:
                    date_str = f"{min_d}_{max_d}"

            center_str = "통합센터"
            if '센터' in df.columns and not df['센터'].dropna().empty:
                center_str = sanitize_filename(df['센터'].dropna().iloc[0])

            # 중복 체크
            is_duplicate = False
            if '영업마감일자' in df.columns and '센터' in df.columns:
                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}" if len(date_str) == 8 else date_str
                cur = conn_b2c.cursor()
                cur.execute("""
                    SELECT COUNT(*) FROM daily_summary 
                    WHERE (영업마감일자 = ? OR 영업마감일자 = ?) AND 센터 LIKE ?
                """, (formatted_date, date_str, f"%{center_str}%"))
                check_count = cur.fetchone()[0]
                
                if check_count > 10:
                    is_duplicate = True

            if is_duplicate:
                # 🔴 중복 파일: [중복_확인필요] 폴더로 이동
                dup_files_count += 1
                seq_num = 1
                new_filename = f"[중복]_{date_str}_{center_str}_{seq_num}.xlsx"
                
                existing_dup_q = f"'{dup_folder_id}' in parents and trashed = false and name contains '{date_str}_{center_str}'"
                existing_dups = safe_list_files(service, existing_dup_q)
                
                if existing_dups:
                    seq_num = len(existing_dups) + 1
                    new_filename = f"[중복]_{date_str}_{center_str}_{seq_num}.xlsx"

                service.files().update(
                    fileId=file_id,
                    addParents=dup_folder_id,
                    removeParents=RAW_FOLDER_ID,
                    body={'name': new_filename},
                    supportsAllDrives=True
                ).execute()

            else:
                # 🟢 신규 파일: 요약 DB 집계 후 [처리완료] 이동
                if '영업마감일자' in df.columns and '센터' in df.columns and '고객사' in df.columns:
                    group_cols = [c for c in ['영업마감일자', '센터', '고객사', '배송속성', '판매처', '출고박스종류', 'SKU명', '바코드'] if c in df.columns]
                    
                    if '송장번호' in df.columns:
                        summary_df = df.groupby(group_cols).agg(
                            출고건수=('송장번호', 'nunique'),
                            총출고수량=('총출고수량', 'sum') if '총출고수량' in df.columns else ('송장번호', 'count')
                        ).reset_index()
                    else:
                        summary_df = df.groupby(group_cols).size().reset_index(name='출고건수')
                        summary_df['총출고수량'] = summary_df['출고건수']

                    summary_df.to_sql('daily_summary', conn_b2c, if_exists='append', index=False)

                seq_num = 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"
                
                existing_query = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '{date_str}_{center_str}'"
                existing_files = safe_list_files(service, existing_query)
                
                if existing_files:
                    seq_num = len(existing_files) + 1
                    new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

                service.files().update(
                    fileId=file_id,
                    addParents=PROCESSED_FOLDER_ID,
                    removeParents=RAW_FOLDER_ID,
                    body={'name': new_filename},
                    supportsAllDrives=True
                ).execute()

                processed_files_count += 1

        except Exception as e:
            print(f"File process error ({orig_name}): {e}")

    # 구글 시트 매칭 (안전한 parse_clean_float 함수 사용)
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

    # DB 다이어트 실행
    try:
        conn_b2c.execute("DELETE FROM shipment_raw WHERE 1=1;")
        conn_b2c.execute("VACUUM;")
        conn_b2c.commit()
    except Exception:
        pass

    conn_b2c.close()

    if dup_files_count > 0:
        err_msg = f"⚠️ 중복 파일 {dup_files_count}건 감지됨 ➔ [중복_확인필요] 폴더로 이동 완료"

    return matched_inbound_count, err_msg
