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
    return build('drive', '3', credentials=credentials)

def get_sheets_service(creds_dict):
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return build('sheets', '4', credentials=credentials)

def sanitize_filename(name_str):
    return str(name_str).replace("/", "_").replace("\\", "_").replace(":", "_").replace("*", "_").replace("?", "_").replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_").strip()

def get_or_create_dup_folder(service):
    """'처리완료' 폴더 내에 [중복_확인필요] 폴더가 없으면 자동 생성 후 ID 반환"""
    try:
        q = f"'{PROCESSED_FOLDER_ID}' in parents and name = '[중복_확인필요]' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=q, fields="files(id)").execute().get('files', [])
        if res:
            return res[0]['id']
        
        folder_metadata = {
            'name': '[중복_확인필요]',
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [PROCESSED_FOLDER_ID]
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
    except Exception:
        return PROCESSED_FOLDER_ID

def rename_existing_processed_files(service):
    """'처리완료' 폴더에 이미 보관 중인 예전 파일들의 이름을 [마감일자_센터명_순번]으로 일괄 변경"""
    try:
        q = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '.xlsx' and not name contains '_1.xlsx' and not name contains '_2.xlsx' and not name contains '_3.xlsx'"
        results = service.files().list(q=q, fields="files(id, name)").execute()
        files = results.get('files', [])

        for f in files:
            file_id = f['id']
            orig_name = f['name']

            # 이미 변경된 포맷의 파일은 패스
            if orig_name.startswith("2026") or orig_name.startswith("2025") or orig_name.startswith("[중복]"):
                continue

            # 파일 읽기
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)

            try:
                df = pd.read_excel(fh)
                date_str = "20261008"
                center_str = "통합센터"

                if '영업마감일자' in df.columns and not df['영업마감일자'].dropna().empty:
                    raw_d = str(df['영업마감일자'].dropna().iloc[0]).replace("-", "").replace("/", "").strip()
                    if len(raw_d) >= 8:
                        date_str = raw_d[:8]
                
                if '센터' in df.columns and not df['센터'].dropna().empty:
                    center_str = sanitize_filename(df['센터'].dropna().iloc[0])

                seq_num = 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"
                
                # 순번 중복 확인
                existing_q = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false and name contains '{date_str}_{center_str}'"
                existing = service.files().list(q=existing_q, fields="files(name)").execute().get('files', [])
                if existing:
                    seq_num = len(existing) + 1
                    new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

                # 이름 업데이트
                service.files().update(fileId=file_id, body={'name': new_filename}).execute()
            except Exception:
                pass
    except Exception as e:
        print(f"Batch rename warning: {e}")

def process_and_update(service, sheets_service=None, progress_callback=None):
    # 0. 기존 '처리완료' 폴더 파일들 이름 일괄 정돈 실행
    rename_existing_processed_files(service)

    # 1. 신규 엑셀 파일 스캔
    query = f"'{RAW_FOLDER_ID}' in parents and trashed = false and name contains '.xlsx'"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

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
            center_str = "통합센터"

            if '영업마감일자' in df.columns and not df['영업마감일자'].dropna().empty:
                raw_d = str(df['영업마감일자'].dropna().iloc[0]).replace("-", "").replace("/", "").strip()
                if len(raw_d) >= 8:
                    date_str = raw_d[:8]
            
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
                existing_dups = service.files().list(q=existing_dup_q, fields="files(name)").execute().get('files', [])
                if existing_dups:
                    seq_num = len(existing_dups) + 1
                    new_filename = f"[중복]_{date_str}_{center_str}_{seq_num}.xlsx"

                service.files().update(
                    fileId=file_id,
                    addParents=dup_folder_id,
                    removeParents=RAW_FOLDER_ID,
                    body={'name': new_filename}
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
                existing_files = service.files().list(q=existing_query, fields="files(name)").execute().get('files', [])
                if existing_files:
                    seq_num = len(existing_files) + 1
                    new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

                service.files().update(
                    fileId=file_id,
                    addParents=PROCESSED_FOLDER_ID,
                    removeParents=RAW_FOLDER_ID,
                    body={'name': new_filename}
                ).execute()

                processed_files_count += 1

        except Exception as e:
            print(f"File process error ({orig_name}): {e}")

    # 구글 시트 매칭
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
                    plt_val = float(row[4]) if len(row) > 4 and row[4] else 0.0
                    box_val = float(row[5]) if len(row) > 5 and row[5] else 0.0

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
