import os
import io
import json
import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
import etl_pipeline
import importlib

importlib.reload(etl_pipeline)

st.set_page_config(page_title="통합 물류 운영 대시보드", layout="wide")

st.markdown("""
<style>
    .sticky-table-container {
        max-height: 600px;
        overflow-y: auto;
        overflow-x: auto;
        border: 1px solid #374151;
        border-radius: 8px;
        background-color: #0e1117;
    }
    .sticky-table {
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
        font-size: 13px;
        color: #e5e7eb;
    }
    .sticky-table th, .sticky-table td {
        padding: 8px 12px;
        text-align: right;
        border-bottom: 1px solid #1f2937;
        border-right: 1px solid #1f2937;
        white-space: nowrap;
        background-color: #0e1117;
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
        position: sticky; left: 0; z-index: 10;
        background-color: #111827 !important; border-right: 1px solid #374151 !important; text-align: left;
    }
    .sticky-table th.freeze-col-2, .sticky-table td.freeze-col-2 {
        position: sticky; left: 140px; z-index: 10;
        background-color: #111827 !important; border-right: 1px solid #374151 !important; text-align: left;
    }
    .sticky-table th.freeze-col-3, .sticky-table td.freeze-col-3,
    .sticky-table th.freeze-col-2-total, .sticky-table td.freeze-col-2-total {
        position: sticky; left: 280px; z-index: 10;
        background-color: #1e1b4b !important; color: #a5b4fc !important;
        font-weight: bold; border-right: 2px solid #4f46e5 !important; text-align: right;
    }
    .sticky-table thead tr th.freeze-col-1, .sticky-table thead tr th.freeze-col-2,
    .sticky-table thead tr th.freeze-col-3, .sticky-table thead tr th.freeze-col-2-total {
        z-index: 30 !important;
    }
    .sticky-table tr.subtotal-row td {
        background-color: #0f172a !important; color: #38bdf8 !important; font-weight: bold;
    }
    .sticky-table .month-sum-col {
        background-color: #172554 !important; color: #60a5fa !important;
        font-weight: bold !important; border-right: 2px solid #2563eb !important; border-left: 2px solid #2563eb !important;
    }
</style>
""", unsafe_allow_html=True)

DB_PATH = "wms_dashboard.db"

def run_sync():
    if "gcp_service_account" in st.secrets:
        try:
            creds_dict = dict(st.secrets["gcp_service_account"])
            service = etl_pipeline.get_drive_service(creds_dict)
            sheets_service = etl_pipeline.get_sheets_service(creds_dict)
            
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(current, total, filename, eta):
                if total > 0:
                    pct = int((current / total) * 100)
                    progress_bar.progress(pct)
                    mins, secs = divmod(eta, 60)
                    eta_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
                    status_text.markdown(f"⏳ **동기화 및 구글 시트 매칭 중 ({pct}%)** - `{current}/{total}`개 완료\n\n📄 **처리 중**: `{filename}` | ⏱️ **남은 시간**: 약 **{eta_str}**")
                else:
                    status_text.info("처리할 새로운 엑셀 파일이 없지만 구글 시트 매칭을 최신화합니다.")

            etl_pipeline.process_and_update(service, sheets_service=sheets_service, progress_callback=update_progress)
            
            progress_bar.empty()
            status_text.empty()
            st.cache_data.clear()
            return True
        except Exception as e:
            st.sidebar.error(f"동기화 에러: {e}")
            return False
    return False

if "initial_synced" not in st.session_state:
    with st.spinner("구글 드라이브 데이터베이스 로드 중..."):
        if "gcp_service_account" in st.secrets:
            try:
                creds_dict = dict(st.secrets["gcp_service_account"])
                service = etl_pipeline.get_drive_service(creds_dict)
                etl_pipeline.download_db_from_drive(service)
            except Exception:
                pass
        st.session_state["initial_synced"] = True

@st.cache_data(ttl=300)
def load_b2c_data():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("SELECT * FROM daily_summary", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df

@st.cache_data(ttl=300)
def load_inbound_data():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("SELECT * FROM inbound_summary", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df

st.title("🏢 센터 통합 물류 운영 대시보드")

if st.sidebar.button("🔄 드라이브 & 구글시트 동기화"):
    run_sync()
    st.rerun()

df_b2c = load_b2c_data()
df_inbound = load_inbound_data()

# 최상단 메인 대메뉴 (센터 현황 / B2C 출고 / 입고 현황)
main_mode = st.radio("📌 운영 모드 선택:", ["🏢 메인 : 센터 종합 현황", "🚚 B2C 출고 현황", "📦 입고 현황"], horizontal=True)

def render_sticky_pivot(df, index_names, key_suffix=""):
    html = ['<div class="sticky-table-container"><table class="sticky-table"><thead><tr>']
    num_indices = len(index_names)
    
    for idx_i, idx_name in enumerate(index_names, 1):
        html.append(f'<th class="freeze-col-{idx_i}">{idx_name}</th>')
    
    cols = [c for c in df.columns]
    for c in cols:
        is_total_col = (c in ['총 출고건수', '총 입고완료수량', '총 PLT수', '총 BOX수'])
        is_m_sum = ("월 합계" in str(c) or ("월" in str(c) and "일자" not in str(c) and "-" not in str(c))) and not is_total_col
        
        if is_total_col:
            freeze_cls = f' class="freeze-col-{num_indices + 1}"' if num_indices == 2 else ' class="freeze-col-2-total"'
            html.append(f'<th{freeze_cls}>{c}</th>')
        else:
            col_cls = ' class="month-sum-col"' if is_m_sum else ''
            html.append(f'<th{col_cls}>{c}</th>')
            
    html.append('</tr></thead><tbody>')
    
    for idx_val, row in df.iterrows():
        is_total = "합계" in str(idx_val)
        is_subtotal = "소계" in str(idx_val)
        
        row_class = ' class="total-row"' if is_total else (' class="subtotal-row"' if is_subtotal else '')
        html.append(f'<tr{row_class}>')
        
        if isinstance(idx_val, tuple):
            for idx_i, v in enumerate(idx_val, 1):
                html.append(f'<td class="freeze-col-{idx_i}">{v}</td>')
        else:
            html.append(f'<td class="freeze-col-1">{idx_val}</td>')
            
        for c_name, val in zip(cols, row):
            is_total_col = (c_name in ['총 출고건수', '총 입고완료수량', '총 PLT수', '총 BOX수'])
            is_m_sum = ("월 합계" in str(c_name) or ("월" in str(c_name) and "-" not in str(c_name))) and not is_total_col
            
            val_str = f"{int(val):,}" if pd.notnull(val) and isinstance(val, (int, float)) else str(val)
            
            if is_total_col:
                freeze_cls = f' class="freeze-col-{num_indices + 1}"' if num_indices == 2 else ' class="freeze-col-2-total"'
                html.append(f'<td{freeze_cls}>{val_str}</td>')
            else:
                td_cls = ' class="month-sum-col"' if is_m_sum else ''
                html.append(f'<td{td_cls}>{val_str}</td>')
                
        html.append('</tr>')
        
    html.append('</tbody></table></div>')
    st.markdown("".join(html), unsafe_allow_html=True)

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='현황데이터')
        
    st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
    st.download_button(
        label="💾 현재 표 데이터 엑셀 다운로드",
        data=excel_buffer.getvalue(),
        file_name=f"센터물류현황_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"dl_table_{key_suffix}"
    )

# --- 1. 메인 센터 종합 현황 모드 ---
if main_mode == "🏢 메인 : 센터 종합 현황":
    st.header("📊 센터 종합 일별 / 월별 실적 요약")
    
    total_b2c_qty = df_b2c['출고건수'].sum() if not df_b2c.empty else 0
    total_inbound_qty = df_inbound['입고완료수량'].sum() if not df_inbound.empty else 0
    total_plt_qty = df_inbound['PLT수'].sum() if not df_inbound.empty else 0
    
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("🚚 총 B2C 출고건수", f"{total_b2c_qty:,} 건")
    kpi2.metric("📦 총 입고 완료 수량", f"{total_inbound_qty:,} EA")
    kpi3.metric("🚜 총 입고 PLT 수", f"{total_plt_qty:,} PLT")
    
    st.markdown("---")
    st.subheader("📋 입고 & B2C 출고 센터별 통합 비교표")
    
    if not df_b2c.empty or not df_inbound.empty:
        df_b2c_sub = df_b2c.groupby(['영업마감일자', '센터'])['출고건수'].sum().reset_index()
        df_b2c_sub.rename(columns={'출고건수': 'B2C출고건수'}, inplace=True)
        
        df_inbound_sub = df_inbound.groupby(['영업마감일자', '센터'])[['입고완료수량', 'PLT수']].sum().reset_index()
        
        merged_main = pd.merge(df_b2c_sub, df_inbound_sub, on=['영업마감일자', '센터'], how='outer').fillna(0)
        
        pivot_main = pd.pivot_table(merged_main, index=['센터'], columns='영업마감일자', values=['B2C출고건수', '입고완료수량'], aggfunc='sum', fill_value=0)
        st.dataframe(pivot_main, use_container_width=True)

# --- 2. B2C 출고 현황 모드 ---
elif main_mode == "🚚 B2C 출고 현황":
    st.header("🚚 B2C 출고 상세 현황")
    if not df_b2c.empty:
        pivot_df = pd.pivot_table(df_b2c, index=['센터'], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
        pivot_df['총 출고건수'] = pivot_df.sum(axis=1)
        cols_order = ['총 출고건수'] + [c for c in pivot_df.columns if c != '총 출고건수']
        render_sticky_pivot(pivot_df[cols_order], ['센터'], key_suffix="b2c_main")

# --- 3. 입고 현황 모드 (구글 시트 연동) ---
elif main_mode == "📦 입고 현황":
    st.header("📦 입고 검수 및 PLT / BOX 정산 현황 (구글 시트 자동 매칭)")
    if not df_inbound.empty:
        col_in1, col_in2 = st.columns(2)
        with col_in1:
            metric_val = st.radio("조회 항목 선택:", ["입고완료수량 (EA)", "PLT수 (PLT)", "BOX수 (BOX)", "입고건수 (건)"], horizontal=True)
        
        col_map_dict = {
            "입고완료수량 (EA)": "입고완료수량",
            "PLT수 (PLT)": "PLT수",
            "BOX수 (BOX)": "BOX수",
            "입고건수 (건)": "입고건수"
        }
        target_val = col_map_dict[metric_val]
        
        pivot_inbound = pd.pivot_table(df_inbound, index=['센터', '고객사'], columns='영업마감일자', values=target_val, aggfunc='sum', fill_value=0)
        total_col_name = f"총 {target_val}"
        pivot_inbound[total_col_name] = pivot_inbound.sum(axis=1)
        
        cols_in_order = [total_col_name] + [c for c in pivot_inbound.columns if c != total_col_name]
        pivot_inbound = pivot_inbound[cols_in_order]
        
        total_series_in = pivot_inbound.sum(axis=0)
        total_df_in = pd.DataFrame([total_series_in.values], columns=pivot_inbound.columns, index=pd.MultiIndex.from_tuples([("★ 전체 합계", "전체")], names=['센터', '고객사']))
        
        final_inbound = pd.concat([total_df_in, pivot_inbound])
        render_sticky_pivot(final_inbound, ['센터', '고객사'], key_suffix="inbound_tab")
    else:
        st.info("입고 데이터가 존재하지 않습니다. 구글 드라이브에 입고요청서 엑셀 파일을 올린 후 [🔄 드라이브 & 구글시트 동기화]를 눌러주세요.")
