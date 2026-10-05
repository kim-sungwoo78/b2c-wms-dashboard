import os
import io
import json
import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta

st.set_page_config(page_title="통합 물류 운영 대시보드", layout="wide")

# etl_pipeline 안전 임포트
try:
    import etl_pipeline
except Exception as e:
    st.error(f"⚠️ etl_pipeline 모듈 로드 오류: {e}")

DB_PATH = "wms_dashboard.db"

# CSS 스티키 테이블 스타일
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
    .sticky-table tr.subtotal-row td {
        background-color: #0f172a !important; color: #38bdf8 !important; font-weight: bold;
    }
    .sticky-table .month-sum-col {
        background-color: #172554 !important; color: #60a5fa !important;
        font-weight: bold !important; border-right: 2px solid #2563eb !important; border-left: 2px solid #2563eb !important;
    }
</style>
""", unsafe_allow_html=True)

def init_local_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 배송속성 TEXT, 판매처 TEXT,
            출고박스종류 TEXT, SKU명 TEXT, 바코드 TEXT, 출고건수 INTEGER, 총출고수량 INTEGER,
            PRIMARY KEY (영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SKU명, 바코드)
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
        conn.commit()
        conn.close()
    except Exception as e:
        st.sidebar.error(f"DB 초기화 오류: {e}")

init_local_db()

def run_sync():
    if "gcp_service_account" in st.secrets:
        try:
            creds_dict = dict(st.secrets["gcp_service_account"])
            service = etl_pipeline.get_drive_service(creds_dict)
            sheets_service = etl_pipeline.get_sheets_service(creds_dict)
            
            status_text = st.sidebar.empty()

            def update_progress(current, total, filename, eta):
                status_text.markdown(f"⏳ **동기화 진행 중 ({current}/{total})**\n\n📄 `{filename}`")

            etl_pipeline.process_and_update(service, sheets_service=sheets_service, progress_callback=update_progress)
            
            status_text.empty()
            st.cache_data.clear()
            st.sidebar.success("✅ 동기화 완료!")
            return True
        except Exception as e:
            st.sidebar.error(f"❌ 동기화 에러: {e}")
            return False
    else:
        st.sidebar.error("시크릿 계정이 없습니다.")
    return False

@st.cache_data(ttl=30)
def load_b2c_data():
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        df = pd.read_sql("SELECT * FROM daily_summary", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=30)
def load_inbound_data():
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        df = pd.read_sql("SELECT * FROM inbound_summary", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

st.title("🏢 센터 통합 물류 운영 대시보드")

if st.sidebar.button("🔄 드라이브 & 구글시트 동기화"):
    if run_sync():
        st.rerun()

df_b2c = load_b2c_data()
df_inbound = load_inbound_data()

# 상단 모드 버튼 UI
if 'main_mode_selection' not in st.session_state:
    st.session_state['main_mode_selection'] = "🏢 메인 : 센터 종합 현황"

btn_col1, btn_col2, btn_col3 = st.columns(3)

with btn_col1:
    btn_type = "primary" if (st.session_state['main_mode_selection'] == "🏢 메인 : 센터 종합 현황") else "secondary"
    if st.button("🏢 메인 : 센터 종합 현황", type=btn_type, use_container_width=True):
        st.session_state['main_mode_selection'] = "🏢 메인 : 센터 종합 현황"
        st.rerun()

with btn_col2:
    btn_type = "primary" if (st.session_state['main_mode_selection'] == "🚚 B2C 출고 현황") else "secondary"
    if st.button("🚚 B2C 출고 현황", type=btn_type, use_container_width=True):
        st.session_state['main_mode_selection'] = "🚚 B2C 출고 현황"
        st.rerun()

with btn_col3:
    btn_type = "primary" if (st.session_state['main_mode_selection'] == "📦 입고 현황") else "secondary"
    if st.button("📦 입고 현황", type=btn_type, use_container_width=True):
        st.session_state['main_mode_selection'] = "📦 입고 현황"
        st.rerun()

main_mode = st.session_state['main_mode_selection']
st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

def render_sticky_pivot(df, index_names, key_suffix=""):
    html = ['<div class="sticky-table-container"><table class="sticky-table"><thead><tr>']
    num_indices = len(index_names)
    
    for idx_i, idx_name in enumerate(index_names, 1):
        html.append(f'<th class="freeze-col-{idx_i}">{idx_name}</th>')
    
    cols = [c for c in df.columns]
    for c in cols:
        is_total_col = ("총 " in str(c) or "합계" in str(c)) and "월" not in str(c)
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
            is_total_col = ("총 " in str(c_name) or "합계" in str(c_name)) and "월" not in str(c_name)
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

def expand_selected_centers(selected_list, all_centers):
    expanded = set()
    c_375_all = [c for c in all_centers if '1층' in str(c) or '375 1' in str(c)]
    c_xfc_all = [c for c in all_centers if 'XFC' in str(c).upper()]

    for item in selected_list:
        if item == "375 소계":
            expanded.update(c_375_all)
        elif item == "XFC 소계":
            expanded.update(c_xfc_all)
        else:
            expanded.add(item)
    return list(expanded)

def inject_monthly_sum_columns(pivot_df):
    date_cols = [c for c in pivot_df.columns if c != '총 출고건수']
    date_cols_sorted = sorted(date_cols)
    
    month_groups = {}
    for d in date_cols_sorted:
        m_key = str(d)[:7]
        month_groups.setdefault(m_key, []).append(d)
        
    new_df = pd.DataFrame(index=pivot_df.index)
    
    if '총 출고건수' in pivot_df.columns:
        new_df['총 출고건수'] = pivot_df['총 출고건수']

    for m_key, m_dates in month_groups.items():
        m_label = f"{m_key[5:7]}월 합계"
        new_df[m_label] = pivot_df[m_dates].sum(axis=1)
        for d in m_dates:
            new_df[d] = pivot_df[d]
        
    return new_df

# ==========================================
# 1. 메인 센터 종합 현황 모드
# ==========================================
if main_mode == "🏢 메인 : 센터 종합 현황":
    st.header("📊 센터 종합 운영 실적 요약")
    
    b2c_months = set(df_b2c['영업마감일자'].str.slice(0, 7).dropna().unique()) if not df_b2c.empty and '영업마감일자' in df_b2c.columns else set()
    inbound_months = set(df_inbound['영업마감일자'].str.slice(0, 7).dropna().unique()) if not df_inbound.empty and '영업마감일자' in df_inbound.columns else set()
    
    all_months = sorted(list(b2c_months.union(inbound_months)))
    month_options = ["전체 기간"] + [f"{m[5:7]}월 ({m})" for m in all_months]
    
    col_filter1, col_filter2 = st.columns([2, 4])
    with col_filter1:
        selected_month_label = st.selectbox("📅 조회 월 선택:", month_options)
        
    filtered_b2c = df_b2c.copy() if not df_b2c.empty else pd.DataFrame()
    filtered_inbound = df_inbound.copy() if not df_inbound.empty else pd.DataFrame()
    
    if selected_month_label != "전체 기간":
        target_m = selected_month_label.split('(')[1].replace(')', '').strip()
        if not filtered_b2c.empty and '영업마감일자' in filtered_b2c.columns:
            filtered_b2c = filtered_b2c[filtered_b2c['영업마감일자'].str.startswith(target_m)]
        if not filtered_inbound.empty and '영업마감일자' in filtered_inbound.columns:
            filtered_inbound = filtered_inbound[filtered_inbound['영업마감일자'].str.startswith(target_m)]

    total_b2c_cnt = filtered_b2c['출고건수'].sum() if not filtered_b2c.empty and '출고건수' in filtered_b2c.columns else 0
    total_inbound_cnt = filtered_inbound['입고건수'].sum() if not filtered_inbound.empty and '입고건수' in filtered_inbound.columns else 0
    total_b2b_cnt = 0

    st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("🚚 B2C 출고건수", f"{total_b2c_cnt:,} 건")
    kpi2.metric("📦 입고건수", f"{total_inbound_cnt:,} 건")
    kpi3.metric("🏭 B2B 건수 (연동 준비중)", f"{total_b2b_cnt:,} 건")
    
    st.markdown("---")
    st.subheader("📋 센터별 운영 항목 종합 비교표")
    
    summary_rows = []
    
    raw_centers = set()
    if not df_b2c.empty and '센터' in df_b2c.columns:
        raw_centers.update(df_b2c['센터'].dropna().unique())
    if not df_inbound.empty and '센터' in df_inbound.columns:
        raw_centers.update(df_inbound['센터'].dropna().unique())
        
    for center_name in sorted(list(raw_centers)):
        b2c_c = filtered_b2c[filtered_b2c['센터'] == center_name]['출고건수'].sum() if not filtered_b2c.empty and '출고건수' in filtered_b2c.columns else 0
        in_c = filtered_inbound[filtered_inbound['센터'] == center_name]['입고건수'].sum() if not filtered_inbound.empty and '입고건수' in filtered_inbound.columns else 0
        b2b_c = 0
        
        summary_rows.append({
            '센터': center_name,
            'B2C 출고건수': b2c_c,
            '입고건수': in_c,
            'B2B 건수': b2b_c,
            '총 작업건수': b2c_c + in_c + b2b_c
        })
        
    if summary_rows:
        df_summary = pd.DataFrame(summary_rows)
        
        total_row = pd.DataFrame([{
            '센터': '★ 전체 합계',
            'B2C 출고건수': df_summary['B2C 출고건수'].sum(),
            '입고건수': df_summary['입고건수'].sum(),
            'B2B 건수': df_summary['B2B 건수'].sum(),
            '총 작업건수': df_summary['총 작업건수'].sum()
        }])
        
        df_summary_final = pd.concat([total_row, df_summary]).reset_index(drop=True)
        
        st.dataframe(
            df_summary_final.style.format({
                'B2C 출고건수': '{:,}',
                '입고건수': '{:,}',
                'B2B 건수': '{:,}',
                '총 작업건수': '{:,}'
            }),
            use_container_width=True,
            hide_index=True
        )

# ==========================================
# 2. B2C 출고 현황 모드
# ==========================================
elif main_mode == "🚚 B2C 출고 현황":
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 센터/고객사별 출고현황", 
        "🚚 배송속성 / 판매처별 현황", 
        "📦 출고박스별 현황",
        "🔍 SKU별 출고량"
    ])

    with tab1:
        st.header("센터 & 고객사별 출고현황 (06시 영업마감 기준)")
        if not df_b2c.empty:
            raw_centers = sorted(list(df_b2c['센터'].dropna().unique()))
            
            center_options = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers):
                center_options.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers):
                center_options.append("XFC 소계")
            center_options.extend(raw_centers)

            col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
            with col1:
                view_mode = st.radio("1. 보기 형식 선택", ["일자별 (일별 상세)", "월별 (월 요약만)"], horizontal=True)
            with col2:
                selected_center_input = st.multiselect("2. 센터 선택 (미선택 시 전체)", center_options, key="tab1_centers")
                expanded_centers = expand_selected_centers(selected_center_input, raw_centers) if selected_center_input else []
            with col3:
                show_client = st.radio("3. 고객사 구분 표시", ["숨김 (센터별 요약)", "보이기 (고객사 상세)"])
            with col4:
                if "보이기" in show_client:
                    available_clients_df = df_b2c[df_b2c['센터'].isin(expanded_centers)] if expanded_centers else df_b2c
                    available_clients = sorted(list(available_clients_df['고객사'].dropna().unique()))
                    clients = st.multiselect("4. 고객사 선택 (미선택 시 전체)", available_clients, key="tab1_clients")
                else:
                    clients = []
                    st.selectbox("4. 고객사 선택", ["고객사 숨김 상태"], disabled=True)

            filtered_df = df_b2c.copy()
            if expanded_centers: 
                filtered_df = filtered_df[filtered_df['센터'].isin(expanded_centers)]
            if "보이기" in show_client and clients: 
                filtered_df = filtered_df[filtered_df['고객사'].isin(clients)]

            group_cols = ['센터']
            if "보이기" in show_client: 
                group_cols.append('고객사')

            if not filtered_df.empty:
                if "월별" in view_mode:
                    filtered_df['연월'] = filtered_df['영업마감일자'].str.slice(0, 7).apply(lambda x: f"{x[5:7]}월 합계")
                    pivot_df = pd.pivot_table(filtered_df, index=group_cols, columns='연월', values='출고건수', aggfunc='sum', fill_value=0)
                    pivot_df['총 출고건수'] = pivot_df.sum(axis=1)
                    cols_order = ['총 출고건수'] + [c for c in pivot_df.columns if c != '총 출고건수']
                    pivot_df = pivot_df[cols_order]
                else:
                    pivot_df = pd.pivot_table(filtered_df, index=group_cols, columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                    pivot_df['총 출고건수'] = pivot_df.sum(axis=1)
                    pivot_df = inject_monthly_sum_columns(pivot_df)

                subtotal_dfs = []
                c_375 = [c for c in pivot_df.index.get_level_values('센터').unique() if '1층' in str(c) or '375 1' in str(c)]
                if c_375:
                    df_375 = pivot_df.loc[pivot_df.index.get_level_values('센터').isin(c_375)]
                    subtotal_dfs.append(df_375)
                    sum_375 = df_375.sum(axis=0)
                    sub_idx_375 = ("375 소계", "소계") if "보이기" in show_client else "375 소계"
                    subtotal_dfs.append(pd.DataFrame([sum_375.values], columns=pivot_df.columns, index=pd.MultiIndex.from_tuples([sub_idx_375], names=group_cols) if "보이기" in show_client else pd.Index([sub_idx_375], name="센터")))

                c_xfc = [c for c in pivot_df.index.get_level_values('센터').unique() if 'XFC' in str(c).upper()]
                if c_xfc:
                    df_xfc = pivot_df.loc[pivot_df.index.get_level_values('센터').isin(c_xfc)]
                    subtotal_dfs.append(df_xfc)
                    sum_xfc = df_xfc.sum(axis=0)
                    sub_idx_xfc = ("XFC 소계", "소계") if "보이기" in show_client else "XFC 소계"
                    subtotal_dfs.append(pd.DataFrame([sum_xfc.values], columns=pivot_df.columns, index=pd.MultiIndex.from_tuples([sub_idx_xfc], names=group_cols) if "보이기" in show_client else pd.Index([sub_idx_xfc], name="센터")))

                c_other = [c for c in pivot_df.index.get_level_values('센터').unique() if c not in c_375 and c not in c_xfc]
                if c_other:
                    df_other = pivot_df.loc[pivot_df.index.get_level_values('센터').isin(c_other)]
                    subtotal_dfs.append(df_other)

                body_df = pd.concat(subtotal_dfs) if subtotal_dfs else pivot_df
                total_series = pivot_df.sum(axis=0)
                total_label = "★ 전체 합계" if "월별" in view_mode else "★ 일별 합계"
                total_idx = pd.MultiIndex.from_tuples([(total_label, "전체")], names=group_cols) if "보이기" in show_client else pd.Index([total_label], name="센터")
                total_df = pd.DataFrame([total_series.values], columns=pivot_df.columns, index=total_idx)

                final_df = pd.concat([total_df, body_df])
                render_sticky_pivot(final_df, group_cols, key_suffix="tab1")

    with tab2:
        st.header("배송 속성 및 판매처별 출고현황")
        if not df_b2c.empty:
            col_t1, col_t2 = st.columns([3, 3])
            with col_t1:
                analysis_type = st.radio("분석 기준 선택", ["배송 속성별", "판매처별"], horizontal=True)
            with col_t2:
                view_mode2 = st.radio("보기 형식 선택", ["일자별 (일별 상세)", "월별 (월 요약만)"], horizontal=True, key="tab2_view")

            target_col = '배송속성' if analysis_type == "배송 속성별" else '판매처'
            df_tab2 = df_b2c.copy()

            if "월별" in view_mode2:
                df_tab2['연월'] = df_tab2['영업마감일자'].str.slice(0, 7).apply(lambda x: f"{x[5:7]}월 합계")
                pivot_df2 = pd.pivot_table(df_tab2, index=[target_col], columns='연월', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_df2['총 출고건수'] = pivot_df2.sum(axis=1)
                cols_order2 = ['총 출고건수'] + [c for c in pivot_df2.columns if c != '총 출고건수']
                pivot_df2 = pivot_df2[cols_order2]
            else:
                pivot_df2 = pd.pivot_table(df_tab2, index=[target_col], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_df2['총 출고건수'] = pivot_df2.sum(axis=1)
                pivot_df2 = inject_monthly_sum_columns(pivot_df2)

            total_series2 = pivot_df2.sum(axis=0)
            total_label2 = "★ 전체 합계" if "월별" in view_mode2 else "★ 일별 합계"
            total_df2 = pd.DataFrame([total_series2.values], columns=pivot_df2.columns, index=pd.Index([total_label2], name=target_col))
            final_df2 = pd.concat([total_df2, pivot_df2])
            render_sticky_pivot(final_df2, [target_col], key_suffix="tab2")

    with tab3:
        st.header("출고박스 규격별 사용 현황")
        if not df_b2c.empty:
            view_mode3 = st.radio("보기 형식 선택", ["일자별 (일별 상세)", "월별 (월 요약만)"], horizontal=True, key="tab3_view")
            df_tab3 = df_b2c.copy()

            if "월별" in view_mode3:
                df_tab3['연월'] = df_tab3['영업마감일자'].str.slice(0, 7).apply(lambda x: f"{x[5:7]}월 합계")
                pivot_df3 = pd.pivot_table(df_tab3, index=['출고박스종류'], columns='연월', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_df3['총 출고건수'] = pivot_df3.sum(axis=1)
                cols_order3 = ['총 출고건수'] + [c for c in pivot_df3.columns if c != '총 출고건수']
                pivot_df3 = pivot_df3[cols_order3]
            else:
                pivot_df3 = pd.pivot_table(df_tab3, index=['출고박스종류'], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_df3['총 출고건수'] = pivot_df3.sum(axis=1)
                pivot_df3 = inject_monthly_sum_columns(pivot_df3)

            total_series3 = pivot_df3.sum(axis=0)
            total_label3 = "★ 전체 합계" if "월별" in view_mode3 else "★ 일별 합계"
            total_df3 = pd.DataFrame([total_series3.values], columns=pivot_df3.columns, index=pd.Index([total_label3], name="출고박스 규격"))
            final_df3 = pd.concat([total_df3, pivot_df3])
            render_sticky_pivot(final_df3, ["출고박스 규격"], key_suffix="tab3")

    with tab4:
        st.header("🔍 SKU별 출고량 (기간 선택 집계)")
        if not df_b2c.empty:
            df_b2c['영업마감일자_dt'] = pd.to_datetime(df_b2c['영업마감일자'], errors='coerce')
            min_date = df_b2c['영업마감일자_dt'].min().date() if not df_b2c['영업마감일자_dt'].isna().all() else datetime.now().date()
            max_date = df_b2c['영업마감일자_dt'].max().date() if not df_b2c['영업마감일자_dt'].isna().all() else datetime.now().date()

            if 'sku_start_date' not in st.session_state:
                st.session_state['sku_start_date'] = min_date
            if 'sku_end_date' not in st.session_state:
                st.session_state['sku_end_date'] = max_date

            st.subheader("📅 빠른 기간 선택")
            btn_col1, btn_col2, btn_col3, btn_col4, btn_col5 = st.columns(5)
            with btn_col1:
                if st.button("오늘", key="btn_today", use_container_width=True):
                    st.session_state['sku_start_date'] = max_date
                    st.session_state['sku_end_date'] = max_date
                    st.rerun()
            with btn_col2:
                if st.button("일주일", key="btn_week", use_container_width=True):
                    st.session_state['sku_start_date'] = max(min_date, max_date - timedelta(days=7))
                    st.session_state['sku_end_date'] = max_date
                    st.rerun()
            with btn_col3:
                if st.button("1개월", key="btn_1m", use_container_width=True):
                    st.session_state['sku_start_date'] = max(min_date, max_date - timedelta(days=30))
                    st.session_state['sku_end_date'] = max_date
                    st.rerun()
            with btn_col4:
                if st.button("3개월", key="btn_3m", use_container_width=True):
                    st.session_state['sku_start_date'] = max(min_date, max_date - timedelta(days=90))
                    st.session_state['sku_end_date'] = max_date
                    st.rerun()
            with btn_col5:
                if st.button("전체 기간", key="btn_all", use_container_width=True):
                    st.session_state['sku_start_date'] = min_date
                    st.session_state['sku_end_date'] = max_date
                    st.rerun()

            date_col1, date_col2 = st.columns(2)
            with date_col1:
                start_date = st.date_input("📅 조회 시작일자:", value=st.session_state['sku_start_date'], key="tab4_start_picker")
                st.session_state['sku_start_date'] = start_date
            with date_col2:
                end_date = st.date_input("📅 조회 종료일자:", value=st.session_state['sku_end_date'], key="tab4_end_picker")
                st.session_state['sku_end_date'] = end_date

            raw_centers_tab4 = sorted(list(df_b2c['센터'].dropna().unique()))
            center_options_tab4 = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers_tab4):
                center_options_tab4.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers_tab4):
                center_options_tab4.append("XFC 소계")
            center_options_tab4.extend(raw_centers_tab4)

            col1, col2 = st.columns(2)
            with col1:
                selected_centers_input_tab4 = st.multiselect("센터 선택 (다중 선택 가능)", center_options_tab4, key="tab4_centers")
                expanded_centers_tab4 = expand_selected_centers(selected_centers_input_tab4, raw_centers_tab4) if selected_centers_input_tab4 else []
            
            filtered_by_center = df_b2c[df_b2c['센터'].isin(expanded_centers_tab4)] if expanded_centers_tab4 else df_b2c
            available_clients = sorted(list(filtered_by_center['고객사'].dropna().unique()))
            
            with col2:
                selected_clients = st.multiselect("고객사 선택 (선택한 센터의 고객사만 표시)", available_clients, key="tab4_clients")
                
            sku_df = df_b2c.copy()
            if start_date and end_date:
                sku_df = sku_df[(sku_df['영업마감일자_dt'].dt.date >= start_date) & (sku_df['영업마감일자_dt'].dt.date <= end_date)]

            if expanded_centers_tab4:
                sku_df = sku_df[sku_df['센터'].isin(expanded_centers_tab4)]
            if selected_clients:
                sku_df = sku_df[sku_df['고객사'].isin(selected_clients)]
                
            if not sku_df.empty:
                sku_summary = sku_df.groupby(['고객사', '바코드', 'SKU명'])[['출고건수', '총출고수량']].sum().reset_index()
                sku_summary = sku_summary.sort_values(by='총출고수량', ascending=False).reset_index(drop=True)
                
                st.dataframe(
                    sku_summary.style.format({'출고건수': '{:,}', '총출고수량': '{:,}'}),
                    use_container_width=True,
                    hide_index=True
                )
                
                excel_buffer_sku = io.BytesIO()
                with pd.ExcelWriter(excel_buffer_sku, engine='openpyxl') as writer:
                    sku_summary.to_excel(writer, index=False, sheet_name='SKU별출고량')
                    
                st.download_button(
                    label="💾 현재 표 데이터 엑셀 다운로드",
                    data=excel_buffer_sku.getvalue(),
                    file_name=f"B2C_SKU별출고량_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_table_tab4"
                )

# ==========================================
# 3. 입고 현황 모드
# ==========================================
elif main_mode == "📦 입고 현황":
    in_tab1, in_tab2 = st.tabs([
        "📊 센터/고객사별 입고 현황",
        "📋 상태별(입고완료/승인대기) 현황"
    ])

    with in_tab1:
        st.header("📦 센터 & 고객사별 입고 현황 (구글 시트 PLT / BOX 매칭)")
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
            
            if target_val in df_inbound.columns:
                pivot_inbound = pd.pivot_table(df_inbound, index=['센터', '고객사'], columns='영업마감일자', values=target_val, aggfunc='sum', fill_value=0)
                total_col_name = f"총 {target_val}"
                pivot_inbound[total_col_name] = pivot_inbound.sum(axis=1)
                
                cols_in_order = [total_col_name] + [c for c in pivot_inbound.columns if c != total_col_name]
                pivot_inbound = pivot_inbound[cols_in_order]
                
                total_series_in = pivot_inbound.sum(axis=0)
                total_df_in = pd.DataFrame([total_series_in.values], columns=pivot_inbound.columns, index=pd.MultiIndex.from_tuples([("★ 전체 합계", "전체")], names=['센터', '고객사']))
                
                final_inbound = pd.concat([total_df_in, pivot_inbound])
                render_sticky_pivot(final_inbound, ['센터', '고객사'], key_suffix="inbound_tab1")
        else:
            st.info("입고 데이터가 존재하지 않습니다. 구글 드라이브에 입고요청서 엑셀 파일을 올린 후 [🔄 드라이브 & 구글시트 동기화]를 눌러주세요.")

    with in_tab2:
        st.header("📋 상태별(입고완료 / 승인대기) 현황")
        if not df_inbound.empty and '상태' in df_inbound.columns:
            pivot_status = pd.pivot_table(df_inbound, index=['센터', '고객사', '상태'], columns='영업마감일자', values='입고완료수량', aggfunc='sum', fill_value=0)
            pivot_status['총 입고완료수량'] = pivot_status.sum(axis=1)
            cols_st_order = ['총 입고완료수량'] + [c for c in pivot_status.columns if c != '총 입고완료수량']
            render_sticky_pivot(pivot_status[cols_st_order], ['센터', '고객사', '상태'], key_suffix="inbound_tab2")
        else:
            st.info("입고 상태 데이터가 없습니다.")
