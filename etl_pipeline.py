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
RAW_FOLDER_ID = "1Iqg4O8fS5E8eO2u0zKq04Jms3zJjM02F"       # 대시보드 업로드 (신규 엑셀)
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

def safe_list_files_by_folder(service, folder_id, is_excel_only=True):
    """404 오류 방지를 위한 3단계 드라이브 쿼리 안전 호환 함수"""
    q = f"'{folder_id}' in parents and trashed = false"
    if is_excel_only:
        q += " and name contains '.xlsx'"

    # 1단계: 일반 드라이브 표준 조회
    try:
        results = service.files().list(q=q, fields="files(id, name, parents, mimeType)").execute()
        return results.get('files', [])
    except Exception:
        pass

    # 2단계: 공유 드라이브 옵션 적용 조회
    try:
        results = service.files().list(
            q=q,
            fields="files(id, name, parents, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])
    except Exception:
        pass

    # 3단계: 전체 드라이브 범주 조회
    try:
        results = service.files().list(
            q=q,
            fields="files(id, name, parents, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora='allDrives'
        ).execute()
        return results.get('files', [])
    except Exception:
        return []

def get_or_create_dup_folder(service):
    """'처리완료' 폴더 내 [중복_확인필요] 폴더 안전 검색 및 생성"""
    try:
        q = f"'{PROCESSED_FOLDER_ID}' in parents and name = '[중복_확인필요]' and trashed = false"
        res = safe_list_files_by_folder(service, PROCESSED_FOLDER_ID, is_excel_only=False)
        dup_folders = [f for f in res if f['name'] == '[중복_확인필요]']
        if dup_folders:
            return dup_folders[0]['id']
        
        folder_metadata = {
            'name': '[중복_확인필요]',
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [PROCESSED_FOLDER_ID]
        }
        try:
            folder = service.files().create(body=folder_metadata, fields='id', supportsAllDrives=True).execute()
        except Exception:
            folder = service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
    except Exception:
        return PROCESSED_FOLDER_ID

def process_and_update(service, sheets_service=None, progress_callback=None):
    # 1. DB 연결 및 테이블 구조 보장
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

    dup_folder_id = get_or_create_dup_folder(service)

    # 2. 신규 로우파일 안전 스캔 ('대시보드 업로드' 및 하위 폴더)
    files = safe_list_files_by_folder(service, RAW_FOLDER_ID, is_excel_only=True)

    # 하위 B2C/B2B 폴더 탐색
    all_sub_items = safe_list_files_by_folder(service, RAW_FOLDER_ID, is_excel_only=False)
    subfolders = [f for f in all_sub_items if f.get('mimeType') == 'application/vnd.google-apps.folder' and f['name'] not in ['처리완료', '[DB전용] 절대 삭제 금지', '[중복_확인필요]']]
    
    for sf in subfolders:
        sub_files = safe_list_files_by_folder(service, sf['id'], is_excel_only=True)
        files.extend(sub_files)

    total_files = len(files)
    processed_files_count = 0
    dup_files_count = 0
    matched_inbound_count = 0
    err_msg = None

    # 3. 로우파일 순회 가공 및 이동
    for idx, f in enumerate(files, 1):
        file_id = f['id']
        orig_name = f['name']
        parent_id = f.get('parents', [RAW_FOLDER_ID])[0]

        if progress_callback:
            progress_callback(idx, total_files, orig_name, f"{idx}/{total_files} 파일 동기화 중")

        # 다운로드
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        try:
            df = pd.read_excel(fh)

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
                elif '배송속성' in sc or '배송구분' in sc: col_map[c] = '배송속성'
                elif '판매처' in sc or '채널' in sc: col_map[c] = '판매처'
                elif '출고박스' in sc or '박스' in sc: col_map[c] = '출고박스종류'
                elif 'SKU' in sc or '상품명' in sc or '품목명' in sc: col_map[c] = 'SKU명'
                elif '바코드' in sc: col_map[c] = '바코드'
                elif '송장' in sc or '운송장' in sc: col_map[c] = '송장번호'

            df_clean = df.rename(columns=col_map)

            if '영업마감일자' not in df_clean.columns: df_clean['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')
            if '센터' not in df_clean.columns: df_clean['센터'] = '통합센터'
            if '고객사' not in df_clean.columns: df_clean['고객사'] = '기타'

            # 마감일자 및 센터 추출
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

            # 중복 체크
            is_duplicate = False
            cur = conn_b2c.cursor()
            formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}" if len(date_str) == 8 else date_str
            cur.execute("""
                SELECT COUNT(*) FROM daily_summary 
                WHERE (영업마감일자 = ? OR 영업마감일자 = ?) AND 센터 LIKE ?
            """, (formatted_date, date_str, f"%{center_str}%"))
            check_cnt = cur.fetchone()[0]

            if check_cnt > 10:
                is_duplicate = True

            if is_duplicate:
                # 🔴 중복 파일 ➔ [중복_확인필요] 폴더로 이동
                dup_files_count += 1
                seq_num = 1
                new_filename = f"[중복]_{date_str}_{center_str}_{seq_num}.xlsx"

                try:
                    service.files().update(
                        fileId=file_id,
                        addParents=dup_folder_id,
                        removeParents=parent_id,
                        body={'name': new_filename},
                        supportsAllDrives=True
                    ).execute()
                except Exception:
                    service.files().update(
                        fileId=file_id,
                        addParents=dup_folder_id,
                        removeParents=parent_id,
                        body={'name': new_filename}
                    ).execute()

            else:
                # 🟢 정상 신규 파일 ➔ 요약 DB 수집 후 [처리완료] 이동
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
                summary_df.to_sql('shipment_raw', conn_b2c, if_exists='append', index=False)

                # 파일명 변경 및 이동
                seq_num = 1
                new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"
                
                ex_files = safe_list_files_by_folder(service, PROCESSED_FOLDER_ID, is_excel_only=True)
                dup_matches = [ef for ef in ex_files if date_str in ef['name'] and center_str in ef['name']]
                if dup_matches:
                    seq_num = len(dup_matches) + 1
                    new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

                try:
                    service.files().update(
                        fileId=file_id,
                        addParents=PROCESSED_FOLDER_ID,
                        removeParents=parent_id,
                        body={'name': new_filename},
                        supportsAllDrives=True
                    ).execute()
                except Exception:
                    service.files().update(
                        fileId=file_id,
                        addParents=PROCESSED_FOLDER_ID,
                        removeParents=parent_id,
                        body={'name': new_filename}
                    ).execute()

                processed_files_count += 1

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

    if dup_files_count > 0:
        err_msg = f"⚠️ 중복 파일 {dup_files_count}건 감지됨 ➔ [중복_확인필요] 폴더로 이동 완료"

    return matched_inbound_count, err_msg
