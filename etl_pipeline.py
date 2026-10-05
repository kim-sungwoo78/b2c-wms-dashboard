import os
import io
import time
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# 구글 드라이브 폴더 ID
TOP_FOLDER_ID = '1UlsDUOZv3QPp19M_vMNptLiDZjEHPPUw'       # 대시보드 업로드
B2C_FOLDER_ID = '1ArGfyeVpZDJYUrdlGNSCrhj734JrqGW9'        # B2C 폴더
INBOUND_FOLDER_ID = '1BzKHxqaUrTFDubvJ7wnfXZqzNHaEvJjp'    # 입고 폴더
B2B_FOLDER_ID = '1wpqrIBC8HnWTU20rShcg0Yvkcc1VIsml'        # B2B 폴더
PROCESSED_FOLDER_ID = '1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ'  # 처리완료 폴더

DB_PATH = 'wms_dashboard.db'
IB_SHEET_ID = '1j3yHXjpOpdYRBI_dFP6TBBG3Q3_vgbMAi3DW4po0SD0' # 이천375 1층 구글 시트 ID

def get_drive_service(creds_dict):
    creds = Credentials.from_service_account_info(
        creds_dict, 
        scopes=[
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/spreadsheets.readonly'
        ]
    )
    return build('drive', 'v3', credentials=creds)

def get_sheets_service(creds_dict):
    creds = Credentials.from_service_account_info(
        creds_dict, 
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    return build('sheets', '4', credentials=creds)

def download_db_from_drive(service):
    try:
        query = f"'{TOP_FOLDER_ID}' in parents and name = '{DB_PATH}' and trashed = false"
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
    except Exception as e:
        print(f"DB Download Error: {e}")
    return False

def upload_db_to_drive(service):
    if not os.path.exists(DB_PATH):
        return
    try:
        query = f"'{TOP_FOLDER_ID}' in parents and name = '{DB_PATH}' and trashed = false"
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
                'parents': [TOP_FOLDER_ID]
            }
            service.files().create(
                body=file_metadata, media_body=media, supportsAllDrives=True
            ).execute()
    except Exception as e:
        print(f"DB Upload Error: {e}")

def fetch_google_sheets_ib(sheets_service):
    try:
        sheet = sheets_service.spreadsheets()
        result = sheet.values().get(spreadsheetId=IB_SHEET_ID, range='IB!A1:Z3000').execute()
        values = result.get('values', [])
        if not values:
            return pd.DataFrame()
        
        headers = [str(h).replace(" ", "").strip() for h in values[0]]
        data = values[1:]
        
        data_fixed = []
        for row in data:
            if len(row) < len(headers):
                row = row + [''] * (len(headers) - len(row))
            data_fixed.append(row[:len(headers)])
            
        df_sheet = pd.DataFrame(data_fixed, columns=headers)
        return df_sheet
    except Exception as e:
        print(f"Sheets Read Error: {e}")
        return pd.DataFrame()

def list_files_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false and name != '{DB_PATH}'"
    results = service.files().list(
        q=query, fields="files(id, name, parents)", supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute()
    return results.get('files', [])

def process_and_update(service, sheets_service=None, progress_callback=None):
    download_db_from_drive(service)

    conn = sqlite3.connect(DB_PATH)
    
    # Raw 및 요약 테이블 생성
    conn.execute("""
    CREATE TABLE IF NOT EXISTS raw_shipments (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, `배송 속성` TEXT,
        `판매 플랫폼` TEXT, `출고 박스` TEXT, SKU명 TEXT, 바코드 TEXT,
        `송장 번호` TEXT, `출고 수량` INTEGER,
        PRIMARY KEY (영업마감일자, `송장 번호`, SKU명, 바코드)
    )
    """)
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SKU명, 바코드)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS raw_inbound (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 상태 TEXT, `입고 번호` TEXT,
        SKU명 TEXT, 바코드 TEXT, `예정 수량` INTEGER, `총 검수 완료 수량` INTEGER, `입고 완료 일시` TEXT,
        PRIMARY KEY (영업마감일자, `입고 번호`, SKU명, 바코드)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS inbound_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 상태 TEXT,
        입고건수 INTEGER, 바코드수 INTEGER, 입고완료수량 INTEGER,
        PLT수 REAL, BOX수 REAL, 파적BOX수 REAL,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 상태)
    )
    """)

    # 하위 폴더별 개별 스캔
    folder_mapping = [
        (INBOUND_FOLDER_ID, 'INBOUND'),
        (B2C_FOLDER_ID, 'B2C'),
        (TOP_FOLDER_ID, 'AUTO')
    ]

    all_target_files = []
    for f_id, category in folder_mapping:
        try:
            files = list_files_in_folder(service, f_id)
            for f in files:
                if f['name'].lower().endswith('.xlsx') or f['name'].lower().endswith('.csv'):
                    f['category'] = category
                    f['source_folder_id'] = f_id
                    all_target_files.append(f)
        except Exception as e:
            print(f"Folder list error ({f_id}): {e}")

    total_count = len(all_target_files)
    start_time = time.time()

    if progress_callback:
        progress_callback(current=0, total=total_count, filename=f"총 {total_count}개 감지됨", eta=0)

    for idx, f in enumerate(all_target_files, 1):
        file_id, file_name = f['id'], f['name']
        category = f['category']
        src_folder = f['source_folder_id']

        if progress_callback:
            elapsed = time.time() - start_time
            avg_time = elapsed / (idx - 1) if idx > 1 else 3.0
            rem_files = total_count - (idx - 1)
            eta_seconds = int(avg_time * rem_files)
            
            progress_callback(
                current=idx, 
                total=total_count, 
                filename=file_name, 
                eta=eta_seconds
            )

        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)

            if file_name.lower().endswith('.csv'):
                df = pd.read_csv(fh)
            else:
                df = pd.read_excel(fh, engine='openpyxl')
            
            df_cols_no_space = [str(c).replace(" ", "").strip() for c in df.columns]

            is_inbound = (category == 'INBOUND') or any(k in "".join(df_cols_no_space) for k in ['입고번호', '총검수완료수량']) or ('입고요청서' in file_name)

            if is_inbound:
                # --- 입고 파일 파싱 ---
                col_map_inbound = {}
                for orig_c in df.columns:
                    clean_c = str(orig_c).replace(" ", "").strip()
                    if '상태' == clean_c: col_map_inbound[orig_c] = '상태'
                    elif '센터' == clean_c: col_map_inbound[orig_c] = '센터'
                    elif '고객사' == clean_c: col_map_inbound[orig_c] = '고객사'
                    elif '입고번호' == clean_c: col_map_inbound[orig_c] = '입고 번호'
                    elif 'SKU명' in clean_c or '상품명' in clean_c: col_map_inbound[orig_c] = 'SKU명'
                    elif '바코드' == clean_c: col_map_inbound[orig_c] = '바코드'
                    elif '예정수량' == clean_c: col_map_inbound[orig_c] = '예정 수량'
                    elif '총검수완료수량' == clean_c: col_map_inbound[orig_c] = '총 검수 완료 수량'
                    elif '입고완료일시' == clean_c: col_map_inbound[orig_c] = '입고 완료 일시'
                    elif '최종변경일시' == clean_c: col_map_inbound[orig_c] = '최종 변경 일시'
                    elif '등록일시' == clean_c: col_map_inbound[orig_c] = '등록 일시'

                df_in = df.rename(columns=col_map_inbound)

                if '상태' not in df_in.columns:
                    df_in['상태'] = '입고 완료'

                s_date = None
                if '입고 완료 일시' in df_in.columns:
                    s_date = df_in['입고 완료 일시']
                elif '최종 변경 일시' in df_in.columns:
                    s_date = df_in['최종 변경 일시']
                elif '등록 일시' in df_in.columns:
                    s_date = df_in['등록 일시']

                if s_date is not None:
                    if '최종 변경 일시' in df_in.columns:
                        s_date = s_date.fillna(df_in['최종 변경 일시'])
                    if '등록 일시' in df_in.columns:
                        s_date = s_date.fillna(df_in['등록 일시'])
                    
                    df_in['dt_temp'] = pd.to_datetime(s_date, errors='coerce')
                    df_in['영업마감일자'] = (df_in['dt_temp'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
                else:
                    df_in['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')

                df_in['영업마감일자'] = df_in['영업마감일자'].fillna(datetime.now().strftime('%Y-%m-%d'))

                target_in_cols = ['영업마감일자', '센터', '고객사', '상태', '입고 번호', 'SKU명', '바코드', '예정 수량', '총 검수 완료 수량', '입고 완료 일시']
                for tc in target_in_cols:
                    if tc not in df_in.columns:
                        df_in[tc] = ''

                for fill_col in ['영업마감일자', '상태', '입고 번호', 'SKU명', '바코드']:
                    df_in[fill_col] = df_in[fill_col].fillna('')

                df_in['예정 수량'] = pd.to_numeric(df_in['예정 수량'], errors='coerce').fillna(0)
                df_in['총 검수 완료 수량'] = pd.to_numeric(df_in['총 검수 완료 수량'], errors='coerce').fillna(0)

                insert_inbound_sql = """
                INSERT OR IGNORE INTO raw_inbound 
                (영업마감일자, 센터, 고객사, 상태, `입고 번호`, SKU명, 바코드, `예정 수량`, `총 검수 완료 수량`, `입고 완료 일시`)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                conn.executemany(insert_inbound_sql, df_in[target_in_cols].to_numpy().tolist())

            else:
                # --- B2C 출고 파일 파싱 ---
                col_map_b2c = {}
                for orig_c in df.columns:
                    clean_c = str(orig_c).replace(" ", "").strip()
                    if '센터' == clean_c: col_map_b2c[orig_c] = '센터'
                    elif '고객사' == clean_c: col_map_b2c[orig_c] = '고객사'
                    elif '배송속성' in clean_c or '배송유형' in clean_c: col_map_b2c[orig_c] = '배송 속성'
                    elif '판매플랫폼' in clean_c or '판매처' in clean_c: col_map_b2c[orig_c] = '판매 플랫폼'
                    elif '출고박스' in clean_c or '박스종류' in clean_c: col_map_b2c[orig_c] = '출고 박스'
                    elif 'SKU명' in clean_c or '상품명' in clean_c: col_map_b2c[orig_c] = 'SKU명'
                    elif '바코드' == clean_c: col_map_b2c[orig_c] = '바코드'
                    elif '송장번호' == clean_c: col_map_b2c[orig_c] = '송장 번호'
                    elif '출고수량' in clean_c or '수량' in clean_c: col_map_b2c[orig_c] = '출고 수량'

                df_b2c_f = df.rename(columns=col_map_b2c)

                date_col = None
                for c in df_b2c_f.columns:
                    if any(k in str(c) for k in ['마감일시', '마감일자', '출고일시', '출고일자', '일시', '일자']):
                        date_col = c
                        break

                if date_col:
                    df_b2c_f['dt_temp'] = pd.to_datetime(df_b2c_f[date_col], errors='coerce')
                    df_b2c_f['영업마감일자'] = (df_b2c_f['dt_temp'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
                else:
                    df_b2c_f['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')

                target_cols = ['영업마감일자', '센터', '고객사', '배송 속성', '판매 플랫폼', '출고 박스', 'SKU명', '바코드', '송장 번호', '출고 수량']
                for tc in target_cols:
                    if tc not in df_b2c_f.columns:
                        df_b2c_f[tc] = ''

                for fill_col in ['영업마감일자', 'SKU명', '바코드', '송장 번호']:
                    df_b2c_f[fill_col] = df_b2c_f[fill_col].fillna('')

                insert_sql = """
                INSERT OR IGNORE INTO raw_shipments 
                (영업마감일자, 센터, 고객사, `배송 속성`, `판매 플랫폼`, `출고 박스`, SKU명, 바코드, `송장 번호`, `출고 수량`)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                conn.executemany(insert_sql, df_b2c_f[target_cols].to_numpy().tolist())

            # 처리 완료 폴더로 안전 이동
            try:
                service.files().update(
                    fileId=file_id,
                    addParents=PROCESSED_FOLDER_ID,
                    removeParents=src_folder,
                    supportsAllDrives=True,
                    fields='id, parents'
                ).execute()
            except Exception as move_e:
                print(f"Move error for {file_name}: {move_e}")

        except Exception as file_e:
            print(f"Error processing file {file_name}: {file_e}")
            continue

    if progress_callback and total_count > 0:
        progress_callback(current=total_count, total=total_count, filename="구글 시트 연동 및 통합 DB 집계 중...", eta=0)

    # 1. B2C 요약 재집계
    conn.execute("DELETE FROM daily_summary;")
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

    # 2. 입고 요약 재집계
    df_sheet = pd.DataFrame()
    if sheets_service:
        df_sheet = fetch_google_sheets_ib(sheets_service)

    df_raw_inbound = pd.read_sql("SELECT * FROM raw_inbound", conn)
    if not df_raw_inbound.empty:
        inbound_grp = df_raw_inbound.groupby(['영업마감일자', '센터', '고객사', '상태', '입고 번호']).agg(
            바코드수=('바코드', 'nunique'),
            입고완료수량=('총 검수 완료 수량', 'first')
        ).reset_index()

        if not df_sheet.empty:
            match_col = '작업번호' if '작업번호' in df_sheet.columns else ('입고번호' if '입고번호' in df_sheet.columns else None)
            if match_col:
                cols_to_keep = [match_col]
                for c in ['PLT', 'BOX', '파적BOX']:
                    if c in df_sheet.columns:
                        cols_to_keep.append(c)
                
                df_sheet_sub = df_sheet[cols_to_keep].copy()
                rename_dict = {match_col: '입고 번호', 'PLT': 'PLT수', 'BOX': 'BOX수', '파적BOX': '파적BOX수'}
                df_sheet_sub.rename(columns=rename_dict, inplace=True)

                for col_c in ['PLT수', 'BOX수', '파적BOX수']:
                    if col_c in df_sheet_sub.columns:
                        df_sheet_sub[col_c] = pd.to_numeric(df_sheet_sub[col_c].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
                    else:
                        df_sheet_sub[col_c] = 0

                inbound_grp = pd.merge(inbound_grp, df_sheet_sub, on='입고 번호', how='left').fillna(0)
            else:
                inbound_grp['PLT수'] = 0
                inbound_grp['BOX수'] = 0
                inbound_grp['파적BOX수'] = 0
        else:
            inbound_grp['PLT수'] = 0
            inbound_grp['BOX수'] = 0
            inbound_grp['파적BOX수'] = 0

        final_inbound_summary = inbound_grp.groupby(['영업마감일자', '센터', '고객사', '상태']).agg(
            입고건수=('입고 번호', 'nunique'),
            바코드수=('바코드수', 'sum'),
            입고완료수량=('입고완료수량', 'sum'),
            PLT수=('PLT수', 'sum'),
            BOX수=('BOX수', 'sum'),
            파적BOX수=('파적BOX수', 'sum')
        ).reset_index()

        conn.execute("DELETE FROM inbound_summary;")
        final_inbound_summary.to_sql('inbound_summary', conn, if_exists='append', index=False)

    conn.commit()
    conn.close()

    upload_db_to_drive(service)
