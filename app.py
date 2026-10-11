import os
import io
import math
import calendar
import sqlite3
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timedelta, date

st.set_page_config(page_title="통합 물류 운영 대시보드", layout="wide", initial_sidebar_state="expanded")

DB_INBOUND_PATH = "wms_inbound.db"

def run_sync():
    if "gcp_service_account" in st.secrets:
        try:
            import etl_pipeline
            creds_dict = dict(st.secrets["gcp_service_account"])
            service = etl_pipeline.get_drive_service(creds_dict)
            sheets_service = etl_pipeline.get_sheets_service(creds_dict)
            
            status_text = st.sidebar.empty()

            def update_progress(current, total, filename, eta):
                status_text.markdown(f"⏳ **동기화 진행 중 ({current}/{total})**\n\n📄 `{filename}`")

            st.cache_data.clear()

            matched_cnt, err_msg = etl_pipeline.process_and_update(service, sheets_service=sheets_service, progress_callback=update_progress)
            
            status_text.empty()
            st.cache_data.clear()

            if matched_cnt > 0:
                st.session_state['sync_msg'] = f"✅ 동기화 완료! (PLT/BOX {matched_cnt:,}건 매칭됨)"
                st.session_state['sync_msg_type'] = "success"
            else:
                st.session_state['sync_msg'] = "✅ 동기화 완료! (최신 DB 갱신 완료)"
                st.session_state['sync_msg_type'] = "info"
            return True
        except Exception as e:
            st.session_state['sync_msg'] = f"❌ 동기화 에러: {e}"
            st.session_state['sync_msg_type'] = "error"
            return False
    else:
        st.session_state['sync_msg'] = "❌ gcp_service_account 시크릿 설정이 없습니다."
        st.session_state['sync_msg_type'] = "error"
    return False

def get_b2c_db_paths(selected_m_str):
    """선택 월에 따른 분할 DB 파일 동적 매칭 (예: wms_b2c_202610.db)"""
    curr_year = datetime.now().strftime('%Y')
    if selected_m_str == "누적":
        db_files = [f for f in os.listdir('.') if f.startswith('wms_b2c_') and f.endswith('.db')]
        return db_files if db_files else ["wms_b2c.db"]
    else:
        m_digit = selected_m_str.replace("월", "").zfill(2)
        target_db = f"wms_b2c_{curr_year}{m_digit}.db"
        if os.path.exists(target_db):
            return [target_db]
        return ["wms_b2c.db"]

@st.cache_data(ttl=5)
def load_shipment_orders(selected_m_str):
    db_paths = get_b2c_db_paths(selected_m_str)
    all_dfs = []
    for db_p in db_paths:
        if os.path.exists(db_p):
            try:
                conn = sqlite3.connect(db_p, timeout=10)
                df_raw = pd.read_sql("""
                    SELECT 영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, COUNT(DISTINCT 송장번호) AS 출고건수
                    FROM shipment_raw
                    GROUP BY 영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류
                """, conn)
                conn.close()
                if not df_raw.empty: all_dfs.append(df_raw)
            except Exception: pass
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()

@st.cache_data(ttl=5)
def load_b2c_sku_data(selected_m_str):
    db_paths = get_b2c_db_paths(selected_m_str)
    all_dfs = []
    for db_p in db_paths:
        if os.path.exists(db_p):
            try:
                conn = sqlite3.connect(db_p, timeout=10)
                df = pd.read_sql("SELECT * FROM daily_summary", conn)
                conn.close()
                if not df.empty: all_dfs.append(df)
            except Exception: pass
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()

@st.cache_data(ttl=5)
def load_b2c_raw_by_range(start_date, end_date, selected_centers):
    """기간 및 센터 조건으로 B2C shipment_raw 조회"""
    all_db_files = [f for f in os.listdir('.') if f.startswith('wms_b2c_') and f.endswith('.db')]
    if not all_db_files and os.path.exists("wms_b2c.db"):
        all_db_files = ["wms_b2c.db"]
    
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')
    
    all_dfs = []
    for db_p in all_db_files:
        if os.path.exists(db_p):
            try:
                conn = sqlite3.connect(db_p, timeout=10)
                query = "SELECT * FROM shipment_raw WHERE 영업마감일자 >= ? AND 영업마감일자 <= ?"
                params = [start_str, end_str]
                
                if selected_centers:
                    placeholders = ','.join(['?'] * len(selected_centers))
                    query += f" AND 센터 IN ({placeholders})"
                    params.extend(selected_centers)
                
                df_raw = pd.read_sql(query, conn, params=params)
                conn.close()
                if not df_raw.empty:
                    all_dfs.append(df_raw)
            except Exception:
                pass
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()

@st.cache_data(ttl=5)
def load_inbound_data():
    if not os.path.exists(DB_INBOUND_PATH): return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_INBOUND_PATH, timeout=10)
        df = pd.read_sql("""
            WITH order_qty AS (
                SELECT 영업마감일자, 센터, 고객사, 상태, 입고번호,
                       MAX(총검수완료수량) AS 고유검수완료수량,
                       MAX(PLT수) AS 고유PLT,
                       MAX(BOX수) AS 고유BOX
                FROM inbound_summary
                GROUP BY 영업마감일자, 센터, 고객사, 상태, 입고번호
            ),
            sku_cnt AS (
                SELECT 영업마감일자, 센터, 고객사, 상태,
                       COUNT(DISTINCT 입고번호) AS 입고건수,
                       COUNT(DISTINCT 바코드) AS 바코드수,
                       COUNT(DISTINCT SKU명) AS SKU개수
                FROM inbound_summary
                GROUP BY 영업마감일자, 센터, 고객사, 상태
            )
            SELECT s.영업마감일자, s.센터, s.고객사, s.상태,
                   s.입고건수, s.바코드수, s.SKU개수,
                   SUM(q.고유검수완료수량) AS 입고완료수량,
                   SUM(q.고유PLT) AS PLT수,
                   SUM(q.고유BOX) AS BOX수
            FROM sku_cnt s
            LEFT JOIN order_qty q 
                   ON s.영업마감일자 = q.영업마감일자 
                  AND s.센터 = q.센터 
                  AND s.고객사 = q.고객사 
                  AND s.상태 = q.상태
            GROUP BY s.영업마감일자, s.센터, s.고객사, s.상태
        """, conn)
        conn.close()
        return df
    except Exception: return pd.DataFrame()

@st.cache_data(ttl=5)
def load_inbound_distinct_counts():
    if not os.path.exists(DB_INBOUND_PATH): return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_INBOUND_PATH, timeout=10)
        df = pd.read_sql("""
            SELECT 영업마감일자, 센터, 고객사, COUNT(DISTINCT 입고번호) AS 입고건수
            FROM inbound_summary
            WHERE 상태 LIKE '%입고완료%' OR 상태 LIKE '%입고 완료%' OR 상태 LIKE '%승인대기%' OR 상태 LIKE '%승인 대기%'
            GROUP BY 영업마감일자, 센터, 고객사
        """, conn)
        conn.close()
        return df
    except Exception: return pd.DataFrame()

# CSS 스티키 테이블 스타일
st.markdown("""
<style>
    .sticky-table-container {
        max-height: 750px; overflow-y: auto; overflow-x: auto;
        border: 1px solid #374151; border-radius: 8px; background-color: #0e1117;
    }
    .sticky-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; color: #e5e7eb; }
    .sticky-table th, .sticky-table td {
        padding: 9px 13px; text-align: center !important;
        border-bottom: 1px solid #1f2937; border-right: 1px solid #1f2937;
        white-space: nowrap; background-color: #0e1117;
    }
    .sticky-table thead tr th {
        position: sticky; top: 0; z-index: 20;
        background-color: #1f2937 !important; color: #9ca3af; font-weight: bold;
    }
    .sticky-table tr.total-row td {
        position: sticky; top: 35px; z-index: 15;
        background-color: #1e293b !important; color: #facc15 !important;
        font-weight: bold; border-bottom: 2px solid #eab308 !important;
    }
    .sticky-table th.freeze-col-1, .sticky-table td.freeze-col-1 {
        position: sticky; left: 0; z-index: 10; background-color: #111827 !important;
    }
    .sticky-table th.freeze-col-2, .sticky-table td.freeze-col-2 {
        position: sticky; left: 140px; z-index: 10; background-color: #111827 !important;
    }
    .sticky-table tr.subtotal-row td { background-color: #0f172a !important; color: #38bdf8 !important; font-weight: bold; }
    .sticky-table .month-sum-col {
        background-color: #172554 !important; color: #60a5fa !important; font-weight: bold !important;
    }
</style>
""", unsafe_allow_html=True)

st.title("🏢 센터 통합 물류 운영 대시보드")

if st.sidebar.button("🔄 드라이브 & 구글시트 동기화"):
    if run_sync(): st.rerun()

if 'sync_msg' in st.session_state and st.session_state['sync_msg']:
    m_type = st.session_state.get('sync_msg_type', 'info')
    if m_type == "success": st.sidebar.success(st.session_state['sync_msg'])
    elif m_type == "error": st.sidebar.error(st.session_state['sync_msg'])
    else: st.sidebar.info(st.session_state['sync_msg'])

if 'main_mode_selection' not in st.session_state:
    st.session_state['main_mode_selection'] = "🏢 메인 : 센터 종합 현황"

btn_col1, btn_col2, btn_col3 = st.columns(3)

with btn_col1:
    btn_type = "primary" if (st.session_state['main_mode_selection'] == "🏢 메인 : 센터 종합 현황") else "secondary"
    if st.button("🏢 메인 : 센터 종합 현황", type=btn_type, use_container_width=True):
        st.session_state['main_mode_selection'] = "🏢 메인 : 센터 종합 현황"
        st.rerun()

with btn
