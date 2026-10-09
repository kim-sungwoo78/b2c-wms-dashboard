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
    """지정 폴더 내 파일 100% 안전 목록 조회"""
    try:
        q = f"'{folder_id}' in parents and trashed = false"
        res = service.files().list(
            q=q,
            fields="files(id, name, parents, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return res.get('files', [])
    except Exception:
        try:
            q = f"'{folder_id}' in parents and trashed = false"
            res = service.files().list(q=q, fields="files(id, name, parents, mimeType)").execute()
            return res.get('files', [])
        except Exception:
            return []

def safe_update_file(service, file_id, add_parents, remove_parents, new_name):
    """안전 파일 이동 및 파일명 변경 실행 함수"""
    body = {'name': new_name}
    try:
        service.files().update(
            fileId=file_id,
            addParents=add_parents,
            removeParents=remove_parents,
            body=body,
            supportsAllDrives=True
        ).execute()
    except Exception:
        try:
            service.files().update(
                fileId=file_id,
                addParents=add_parents,
                removeParents=remove_parents,
                body=body
            ).execute()
        except Exception as e:
            print(f"File update error for {file_id}: {e}")

def process_and_update(service, sheets_service=None, progress_callback=None):
    # 1. DB 테이블 준비
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

    # 2. ★ [핵심: 신규 업로드 폴더 + 이미 '처리완료' 폴더에 들어간 모든 파일까지 전수 수집] ★
    target_folder_ids = [RAW_FOLDER_ID, PROCESSED_FOLDER_ID]
    
    # B2C 등 하위 폴더 ID 수집
    raw_sub_items = safe_list_files(service, RAW_FOLDER_ID)
    subfolders = [f for f in raw_sub_items if f.get('mimeType') == 'application/vnd.google-apps.folder' and f['name'] not in ['처리완료', '[DB전용] 절대 삭제 금지', '[중복_확인필요]']]
    for sf in subfolders:
        target_folder_ids.append(sf['id'])

    all_target_files = []
    for f_id in target_folder_ids:
        items = safe_list_files(service, f_id)
        for item in items:
            if item.get('mimeType') != 'application/vnd.google-apps.folder':
                fname = item['name'].lower()
                if (fname.endswith('.xlsx') or fname.endswith('.xls')) and not fname.startswith('~$') and not fname.startswith('[중복]'):
                    all_target_files.append((item, f_id))

    total_files = len(all_target_files)
    processed_cnt = 0
    matched_inbound_count = 0
    err_msg = None

    # 3. 전수 로우파일 순회 및 DB 완벽 재생성 + 파일명 정돈
    for idx, (f_info, current_folder_id) in enumerate(all_target_files, 1):
        file_id = f_info['id']
        orig_name = f_info['name']

        if progress_callback:
            progress_callback(idx, total_files, orig_name, f"{idx}/{total_files} 파일 동기화 중")

        # 파일 다운로드
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        try:
            # 헤더 파싱
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

            # 마감일자 및 센터명 안전 추출
            date_str = datetime.now().strftime('%Y%m%d')
            try:
                clean_dates = df_clean['영업마감일자'].dropna().astype(str).str.replace("-", "").str.replace("/", "").str.strip()
                clean_dates = clean_dates[clean_dates.str.len() >= 8]
                if not clean_dates.empty:
                    min_d = clean_dates.min()[:8]
                    max_d = clean_dates.max()[:8]
                    date_str = min_d if min_d == max_d else f"{min_d}_{max_d}"
            except Exception:
                pass

            center_str = "통합센터"
            try:
                center_val = df_clean['센터'].dropna().iloc[0] if not df_clean['센터'].dropna().empty else "통합센터"
                center_str = sanitize_filename(center_val)
            except Exception:
                pass

            # DB 집계 저장 (daily_summary + shipment_raw)
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

            # 파일명 자동 정돈 및 '처리완료' 폴더로 이동
            seq_num = idx
            new_filename = f"{date_str}_{center_str}_{seq_num}.xlsx"

            if current_folder_id != PROCESSED_FOLDER_ID:
                safe_update_file(service, file_id, PROCESSED_FOLDER_ID, current_folder_id, new_filename)
            else:
                if orig_name != new_filename:
                    body = {'name': new_filename}
                    try:
                        service.files().update(fileId=file_id, body=body, supportsAllDrives=True).execute()
                    except Exception:
                        service.files().update(fileId=file_id, body=body).execute()

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

    if processed_cnt > 0:
        err_msg = f"🎉 B2C 파일 {processed_cnt}건 가공 및 DB 재구축 완료!"

    return matched_inbound_count, err_msg
