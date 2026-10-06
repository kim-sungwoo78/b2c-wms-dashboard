import os
import io
import time
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import openpyxl

TOP_FOLDER_ID = '1UlsDUOZv3QPp19M_vMNptLiDZjEHPPUw'       # 대시보드 업로드
B2C_FOLDER_ID = '1ArGfyeVpZDJYUrdlGNSCrhj734JrqGW9'        # B2C 폴더
INBOUND_FOLDER_ID = '1BzKHxqaUrTFDubvJ7wnfXZqzNHaEvJjp'    # 입고 폴더
B2B_FOLDER_ID = '1wpqrIBC8HnWTU20rShcg0Yvkcc1VIsml'        # B2B 폴더
PROCESSED_FOLDER_ID = '1RiUOVDt8VEgOnePr_bje-ZPuzqlTYOXZ'  # 처리완료 폴더

DB_PATH = 'wms_dashboard.db'
IB_SHEET_ID = '1j3yHXjpOpdYRBI_dFP6TBBG3Q3_vgbMAi3DW4po0SD0'

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
    return build('sheets', 'v4', credentials=creds)

def download_db_from_drive(service):
    try:
        query = f"'{TOP_FOLDER_ID}' in parents and name = '{DB_PATH}' and trashed = false"
        results = service.files().list(
            q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
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
            q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
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

def list_files_in_folder(service, folder_id):
    try:
        query = f"'{folder_id}' in parents and trashed = false and name != '{DB_PATH}'"
        results = service.files().list(
            q=query, fields="files(id, name, parents)", supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])
    except Exception as e:
        print(f"Folder list error ({folder_id}): {e}")
        return []

def read_excel_fast(fh):
    try:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
        target_sheet = wb.active
        
        for sheet_name in wb.sheetnames:
            s = wb[sheet_name]
            for row in list(s.iter_rows(max_row=5, values_only=True)):
                row_str = " ".join([str(v) for v in row if v is not None])
                if any(k in row_str for k in ['송장 번호', '송장번호', '마감 일시', '마감일시', 'SKU명', '입고번호']):
                    target_sheet = s
                    break
            else:
                continue
            break

        rows = target_sheet.iter_rows(values_only=True)
        headers = list(next(rows))
        
        clean_headers = []
        counts = {}
        for h in headers:
            h_str = str(h).strip() if h is not None else "Unnamed"
            counts[h_str] = counts.get(h_str, 0) + 1
            clean_headers.append(f"{h_str}_{counts[h_str]}" if counts[h_str] > 1 else h_str)

        data = [r for r in rows if any(v is not None for v in r)]
        df = pd.DataFrame(data, columns=clean_headers)
        wb.close()
        return df
    except Exception:
        fh.seek(0)
        return pd.read_excel(fh, engine='openpyxl')

def process_and_update(service, sheets_service=None, progress_callback=None):
    download_db_from_drive(service)

    conn = sqlite3.connect(DB_PATH, timeout=30)
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shipment_raw (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
        출고박스종류 TEXT, 송장번호 TEXT, 마감일시 TEXT, 마감자 TEXT, 주문일시 TEXT,
        결제일시 TEXT, 등록일시 TEXT, 할당일시 TEXT, 출력일시 TEXT, 브랜드 TEXT,
        배송계약태그 TEXT, 주문번호 TEXT, 개별주문번호 TEXT, 피킹지시서번호 TEXT,
        품고추적번호 TEXT, CS TEXT,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, 송장번호)
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
    CREATE TABLE IF NOT EXISTS inbound_summary (
        영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 상태 TEXT, 입고번호 TEXT,
        입고방법 TEXT, SKU명 TEXT, 바코드 TEXT, 소비기한 TEXT, 로트 TEXT,
        기본로케이션 TEXT, 예정수량 INTEGER, 요청SKU수량 INTEGER, 총예정수량 INTEGER,
        총검수완료수량 INTEGER, PLT수 REAL, BOX수 REAL, 파적BOX수 REAL, 등록일시 TEXT, 변경자 TEXT,
        최종변경일시 TEXT, 입고완료일시 TEXT,
        PRIMARY KEY (영업마감일자, 센터, 고객사, 상태, 입고번호, SKU명, 바코드)
    )
    """)

    folder_mapping = [
        (INBOUND_FOLDER_ID, 'INBOUND'),
        (B2C_FOLDER_ID, 'B2C'),
        (TOP_FOLDER_ID, 'AUTO')
    ]

    all_target_files = []
    for f_id, category in folder_mapping:
        files = list_files_in_folder(service, f_id)
        for f in files:
            if f['name'].lower().endswith('.xlsx') or f['name'].lower().endswith('.csv'):
                f['category'] = category
                f['source_folder_id'] = f_id
                all_target_files.append(f)

    total_count = len(all_target_files)
    new_files_processed = False
    error_logs = []

    # ★ 10/4일 로우 재집계 시 기존 10/4 DB 찌꺼기 선별 삭제 플래그
    cleaned_dates = set()

    for idx, f in enumerate(all_target_files, 1):
        file_id, file_name = f['id'], f['name']
        category = f['category']
        src_folder = f['source_folder_id']

        if progress_callback:
            progress_callback(current=idx, total=total_count, filename=file_name, eta=0)

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
                df = read_excel_fast(fh)
            
            df_cols_no_space = [str(c).replace(" ", "").strip() for c in df.columns]
            is_inbound = (category == 'INBOUND') or any(k in "".join(df_cols_no_space) for k in ['입고번호', '총검수완료수량', '입고방법']) or ('입고요청서' in file_name)

            if is_inbound:
                col_map_inbound = {}
                for orig_c in df.columns:
                    clean_c = str(orig_c).replace(" ", "").strip()
                    if '상태' == clean_c: col_map_inbound[orig_c] = '상태'
                    elif '센터' == clean_c: col_map_inbound[orig_c] = '센터'
                    elif '고객사' == clean_c: col_map_inbound[orig_c] = '고객사'
                    elif '입고번호' == clean_c: col_map_inbound[orig_c] = '입고번호'
                    elif '입고방법' in clean_c: col_map_inbound[orig_c] = '입고방법'
                    elif 'SKU명' in clean_c or '상품명' in clean_c: col_map_inbound[orig_c] = 'SKU명'
                    elif '바코드' == clean_c: col_map_inbound[orig_c] = '바코드'
                    elif '소비기한' in clean_c or '유통기한' in clean_c: col_map_inbound[orig_c] = '소비기한'
                    elif '로트' in clean_c or 'LOT' in clean_c.upper(): col_map_inbound[orig_c] = '로트'
                    elif '로케이션' in clean_c: col_map_inbound[orig_c] = '기본로케이션'
                    elif '예정수량' == clean_c: col_map_inbound[orig_c] = '예정수량'
                    elif '요청SKU' in clean_c or '요청sku' in clean_c: col_map_inbound[orig_c] = '요청SKU수량'
                    elif '총예정수량' in clean_c: col_map_inbound[orig_c] = '총예정수량'
                    elif '총검수완료수량' in clean_c or '검수완료' in clean_c: col_map_inbound[orig_c] = '총검수완료수량'
                    elif '등록일시' == clean_c: col_map_inbound[orig_c] = '등록일시'
                    elif '변경자' in clean_c: col_map_inbound[orig_c] = '변경자'
                    elif '최종변경일시' in clean_c: col_map_inbound[orig_c] = '최종변경일시'
                    elif '입고완료일시' in clean_c: col_map_inbound[orig_c] = '입고완료일시'

                df_in = df.rename(columns=col_map_inbound)
                df_in = df_in.loc[:, ~df_in.columns.duplicated()]

                if '상태' not in df_in.columns: df_in['상태'] = '입고 완료'

                s_date = None
                for candidate in ['입고완료일시', '최종변경일시', '등록일시']:
                    if candidate in df_in.columns:
                        s_date = df_in[candidate]
                        break

                if s_date is not None:
                    df_in['dt_temp'] = pd.to_datetime(s_date, errors='coerce')
                    df_in['영업마감일자'] = (df_in['dt_temp'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
                else:
                    df_in['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')

                df_in['영업마감일자'] = df_in['영업마감일자'].fillna(datetime.now().strftime('%Y-%m-%d'))

                req_cols_ib = [
                    '영업마감일자', '센터', '고객사', '상태', '입고번호', '입고방법', 'SKU명', '바코드',
                    '소비기한', '로트', '기본로케이션', '예정수량', '요청SKU수량', '총예정수량',
                    '총검수완료수량', '등록일시', '변경자', '최종변경일시', '입고완료일시'
                ]
                for tc in req_cols_ib:
                    if tc not in df_in.columns: df_in[tc] = '미지정' if '수량' not in tc else 0
                    df_in[tc] = df_in[tc].fillna('미지정' if '수량' not in tc else 0)

                for num_c in ['예정수량', '요청SKU수량', '총예정수량', '총검수완료수량']:
                    df_in[num_c] = pd.to_numeric(df_in[num_c], errors='coerce').fillna(0)

                for _, row_in in df_in.iterrows():
                    conn.execute("""
                    INSERT OR REPLACE INTO inbound_summary
                    (영업마감일자, 센터, 고객사, 상태, 입고번호, 입고방법, SKU명, 바코드, 소비기한, 로트,
                     기본로케이션, 예정수량, 요청SKU수량, 총예정수량, 총검수완료수량, PLT수, BOX수, 파적BOX수, 등록일시, 변경자,
                     최종변경일시, 입고완료일시)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0, 0.0, ?, ?, ?, ?)
                    """, (
                        row_in['영업마감일자'], row_in['센터'], row_in['고객사'], row_in['상태'],
                        str(row_in['입고번호']), str(row_in['입고방법']), str(row_in['SKU명']), str(row_in['바코드']),
                        str(row_in['소비기한']), str(row_in['로트']), str(row_in['기본로케이션']),
                        int(row_in['예정수량']), int(row_in['요청SKU수량']), int(row_in['총예정수량']), int(row_in['총검수완료수량']),
                        str(row_in['등록일시']), str(row_in['변경자']), str(row_in['최종변경일시']), str(row_in['입고완료일시'])
                    ))

            else:
                col_map_b2c = {}
                for orig_c in df.columns:
                    clean_c = str(orig_c).replace(" ", "").strip()
                    if '상세' in clean_c: 
                        continue
                    if '센터' in clean_c: col_map_b2c[orig_c] = '센터'
                    elif '고객사' in clean_c: col_map_b2c[orig_c] = '고객사'
                    elif '배송속성' in clean_c or '배송유형' in clean_c: col_map_b2c[orig_c] = '배송속성'
                    elif '판매플랫폼' in clean_c or '판매처' in clean_c: col_map_b2c[orig_c] = '판매처'
                    elif '출고박스' in clean_c or '박스' in clean_c: col_map_b2c[orig_c] = '출고박스종류'
                    elif 'SKU' in clean_c or '상품명' in clean_c: col_map_b2c[orig_c] = 'SKU명'
                    elif '바코드' in clean_c: col_map_b2c[orig_c] = '바코드'
                    elif '송장번호' in clean_c or '운송장' in clean_c: col_map_b2c[orig_c] = '송장번호'
                    elif '마감일시' in clean_c or '마감일' in clean_c: col_map_b2c[orig_c] = '마감일시'
                    elif '마감자' == clean_c: col_map_b2c[orig_c] = '마감자'
                    elif '주문일시' in clean_c or '주문일' in clean_c: col_map_b2c[orig_c] = '주문일시'
                    elif '결제일시' in clean_c or '결제일' in clean_c: col_map_b2c[orig_c] = '결제일시'
                    elif '등록일시' in clean_c or '등록일' in clean_c: col_map_b2c[orig_c] = '등록일시'
                    elif '할당일시' in clean_c or '할당일' in clean_c: col_map_b2c[orig_c] = '할당일시'
                    elif '출력일시' in clean_c or '출력일' in clean_c: col_map_b2c[orig_c] = '출력일시'
                    elif '브랜드' in clean_c: col_map_b2c[orig_c] = '브랜드'
                    elif '배송계약' in clean_c or '태그' in clean_c: col_map_b2c[orig_c] = '배송계약태그'
                    elif '개별주문' in clean_c or '개별주문번호' in clean_c: col_map_b2c[orig_c] = '개별주문번호'
                    elif '주문번호' in clean_c: col_map_b2c[orig_c] = '주문번호'
                    elif '피킹지시서' in clean_c: col_map_b2c[orig_c] = '피킹지시서번호'
                    elif '품고추적' in clean_c or '품고' in clean_c: col_map_b2c[orig_c] = '품고추적번호'
                    elif 'CS' in clean_c or 'cs' in clean_c: col_map_b2c[orig_c] = 'CS'
                    elif '출고수량' in clean_c or '수량' in clean_c or '수' in clean_c: col_map_b2c[orig_c] = '총출고수량'

                df_b2c_f = df.rename(columns=col_map_b2c)
                df_b2c_f = df_b2c_f.loc[:, ~df_b2c_f.columns.duplicated()]
                df_b2c_f = df_b2c_f.ffill()

                date_col_name = None
                for c in ['마감일시', '주문일시', '등록일시']:
                    if c in df_b2c_f.columns:
                        date_col_name = c
                        break

                if date_col_name is not None:
                    raw_date_data = df_b2c_f[date_col_name]
                    df_b2c_f['dt_temp'] = pd.to_datetime(raw_date_data, errors='coerce')
                    df_b2c_f['영업마감일자'] = (df_b2c_f['dt_temp'] - pd.Timedelta(hours=6)).dt.strftime('%Y-%m-%d')
                else:
                    df_b2c_f['영업마감일자'] = datetime.now().strftime('%Y-%m-%d')

                df_b2c_f['영업마감일자'] = df_b2c_f['영업마감일자'].fillna(datetime.now().strftime('%Y-%m-%d'))

                req_cols_b2c = [
                    '센터', '고객사', '배송속성', '판매처', '출고박스종류', 'SKU명', '바코드', '송장번호',
                    '마감일시', '마감자', '주문일시', '결제일시', '등록일시', '할당일시', '출력일시',
                    '브랜드', '배송계약태그', '주문번호', '개별주문번호', '피킹지시서번호', '품고추적번호', 'CS'
                ]
                for tc in req_cols_b2c:
                    if tc not in df_b2c_f.columns: df_b2c_f[tc] = '미지정'
                    df_b2c_f[tc] = df_b2c_f[tc].fillna('미지정')

                if '총출고수량' not in df_b2c_f.columns: df_b2c_f['총출고수량'] = 1
                df_b2c_f['총출고수량'] = pd.to_numeric(df_b2c_f['총출고수량'], errors='coerce').fillna(1)

                valid_mask = ~df_b2c_f['송장번호'].astype(str).str.contains('상세|보기|미지정', na=False)
                df_b2c_valid = df_b2c_f[valid_mask]

                # ★ [핵심] 처리할 파일의 영업마감일자에 대해 기존 DB 데이터를 선별 삭제(Clean Replace)
                target_dates = df_b2c_valid['영업마감일자'].unique()
                for d_val in target_dates:
                    if d_val not in cleaned_dates:
                        conn.execute("DELETE FROM shipment_raw WHERE 영업마감일자 = ?", (d_val,))
                        conn.execute("DELETE FROM daily_summary WHERE 영업마감일자 = ?", (d_val,))
                        cleaned_dates.add(d_val)

                shipment_distinct = df_b2c_valid[
                    ['영업마감일자', '센터', '고객사', '배송속성', '판매처', '출고박스종류', '송장번호',
                     '마감일시', '마감자', '주문일시', '결제일시', '등록일시', '할당일시', '출력일시',
                     '브랜드', '배송계약태그', '주문번호', '개별주문번호', '피킹지시서번호', '품고추적번호', 'CS']
                ].drop_duplicates(subset=['영업마감일자', '센터', '고객사', '배송속성', '판매처', '출고박스종류', '송장번호'])

                for _, row_s in shipment_distinct.iterrows():
                    conn.execute("""
                    INSERT OR REPLACE INTO shipment_raw
                    (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, 송장번호,
                     마감일시, 마감자, 주문일시, 결제일시, 등록일시, 할당일시, 출력일시,
                     브랜드, 배송계약태그, 주문번호, 개별주문번호, 피킹지시서번호, 품고추적번호, CS)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        row_s['영업마감일자'], row_s['센터'], row_s['고객사'], row_s['배송속성'],
                        row_s['판매처'], row_s['출고박스종류'], row_s['송장번호'], str(row_s['마감일시']),
                        str(row_s['마감자']), str(row_s['주문일시']), str(row_s['결제일시']),
                        str(row_s['등록일시']), str(row_s['할당일시']), str(row_s['출력일시']),
                        str(row_s['브랜드']), str(row_s['배송계약태그']), str(row_s['주문번호']),
                        str(row_s['개별주문번호']), str(row_s['피킹지시서번호']), str(row_s['품고추적번호']), str(row_s['CS'])
                    ))

                b2c_sum = df_b2c_valid.groupby(
                    ['영업마감일자', '센터', '고객사', '배송속성', '판매처', '출고박스종류', 'SKU명', '바코드']
                ).agg(
                    출고건수=('송장번호', 'nunique'),
                    총출고수량=('총출고수량', 'sum')
                ).reset_index()

                for _, row_b2c in b2c_sum.iterrows():
                    conn.execute("""
                    INSERT OR REPLACE INTO daily_summary
                    (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SKU명, 바코드, 출고건수, 총출고수량)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        row_b2c['영업마감일자'], row_b2c['센터'], row_b2c['고객사'], row_b2c['배송속성'], row_b2c['판매처'],
                        row_b2c['출고박스종류'], row_b2c['SKU명'], row_b2c['바코드'],
                        int(row_b2c['출고건수']), int(row_b2c['총출고수량'])
                    ))

            try:
                service.files().update(
                    fileId=file_id,
                    addParents=PROCESSED_FOLDER_ID,
                    removeParents=src_folder,
                    supportsAllDrives=True,
                    fields='id, parents'
                ).execute()
            except Exception as move_e:
                error_logs.append(f"이동 실패 ({file_name}): {move_e}")

            conn.commit()
            new_files_processed = True
        except Exception as file_e:
            error_logs.append(f"파싱 실패 ({file_name}): {file_e}")
            continue

    conn.close()

    if new_files_processed:
        upload_db_to_drive(service)

    if error_logs:
        raise Exception(" | ".join(error_logs))
