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
    for idx_i, idx_name in enumerate(index_names, 1): html.append(f'<th class="freeze-col-{idx_i}">{idx_name}</th>')
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
            for idx_i, v in enumerate(idx_val, 1): html.append(f'<td class="freeze-col-{idx_i}">{v}</td>')
        else: html.append(f'<td class="freeze-col-1">{idx_val}</td>')
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
        label="💾 현재 표 데이터 엑셀 다운로드", data=excel_buffer.getvalue(),
        file_name=f"센터물류현황_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"dl_table_{key_suffix}"
    )

def expand_selected_centers(selected_list, all_centers):
    expanded = set()
    c_375_all = [c for c in all_centers if '1층' in str(c) or '375 1' in str(c)]
    c_xfc_all = [c for c in all_centers if 'XFC' in str(c).upper()]
    for item in selected_list:
        if item == "375 소계": expanded.update(c_375_all)
        elif item == "XFC 소계": expanded.update(c_xfc_all)
        else: expanded.add(item)
    return list(expanded)

def smart_fold_pivot_columns(pivot_df, expanded_months):
    all_cols = [c for c in pivot_df.columns if not ("총 " in str(c) and "월" not in str(c))]
    date_cols_sorted = sorted([c for c in all_cols if len(str(c)) == 10 and str(c)[4] == '-' and str(c)[7] == '-'])
    month_groups = {}
    for d in date_cols_sorted:
        m_key = str(d)[:7]
        m_label = f"{int(m_key[5:7])}월"
        month_groups.setdefault(m_key, {'label': m_label, 'dates': []})['dates'].append(d)
    new_df = pd.DataFrame(index=pivot_df.index)
    total_cols = [c for c in pivot_df.columns if "총 " in str(c) and "월" not in str(c)]
    for tc in total_cols: new_df[tc] = pivot_df[tc]
    for m_key, info in sorted(month_groups.items()):
        m_label = info['label']
        m_sum_col_name = f"{m_key[5:7]}월 합계"
        m_dates = info['dates']
        new_df[m_sum_col_name] = pivot_df[m_dates].sum(axis=1)
        if m_label in expanded_months or f"{int(m_key[5:7]):02d}월" in expanded_months or m_key in expanded_months:
            for d in m_dates: new_df[d] = pivot_df[d]
    return new_df

def render_month_button_bar(session_key_selected, title_label="🗓️ 기준 월 선택:", allow_nujak=False, multi_select=False):
    st.markdown(f"<p style='font-size:14px; font-weight:bold; margin-bottom:5px;'>{title_label}</p>", unsafe_allow_html=True)
    adjusted_today = datetime.now() - timedelta(days=1)
    default_month_str = f"{adjusted_today.month}월"

    if session_key_selected not in st.session_state:
        st.session_state[session_key_selected] = [default_month_str] if multi_select else default_month_str

    months_list = [f"{i}월" for i in range(1, 13)]
    if allow_nujak: months_list.append("누적")

    cols = st.columns(len(months_list))
    for idx, m_name in enumerate(months_list):
        with cols[idx]:
            is_selected = (m_name in st.session_state[session_key_selected]) if multi_select else (st.session_state[session_key_selected] == m_name)
            btn_style = "primary" if is_selected else "secondary"
            if st.button(m_name, key=f"btn_{session_key_selected}_{m_name}", type=btn_style, use_container_width=True):
                if multi_select:
                    curr_list = list(st.session_state[session_key_selected])
                    if m_name in curr_list: curr_list.remove(m_name)
                    else: curr_list.append(m_name)
                    st.session_state[session_key_selected] = curr_list
                else: st.session_state[session_key_selected] = m_name
                st.rerun()

    return st.session_state[session_key_selected]

# 메인 종합 현황 모드
if main_mode == "🏢 메인 : 센터 종합 현황":
    st.header("📊 센터 종합 운영 실적 요약")
    selected_m = render_month_button_bar(session_key_selected="main_selected_month", title_label="🗓️ 기준 월 선택:", allow_nujak=True, multi_select=False)

    df_b2c_orders = load_shipment_orders(selected_m)
    df_inbound_distinct = load_inbound_distinct_counts()

    raw_centers = set()
    if not df_b2c_orders.empty and '센터' in df_b2c_orders.columns: raw_centers.update(df_b2c_orders['센터'].dropna().unique())
    if not df_inbound_distinct.empty and '센터' in df_inbound_distinct.columns: raw_centers.update(df_inbound_distinct['센터'].dropna().unique())
    sorted_centers = sorted(list(raw_centers))
    
    center_options_main = []
    if any('1층' in str(c) or '375 1' in str(c) for c in sorted_centers): center_options_main.append("375 소계")
    if any('XFC' in str(c).upper() for c in sorted_centers): center_options_main.append("XFC 소계")
    center_options_main.extend(sorted_centers)

    selected_centers_filter = st.multiselect("🏢 센터 선택 (미선택 시 전체):", center_options_main, default=[], key="main_center_filter")
    expanded_main_centers = expand_selected_centers(selected_centers_filter, sorted_centers) if selected_centers_filter else []

    filtered_b2c = df_b2c_orders.copy() if not df_b2c_orders.empty else pd.DataFrame()
    filtered_inbound = df_inbound_distinct.copy() if not df_inbound_distinct.empty else pd.DataFrame()

    if expanded_main_centers:
        if not filtered_b2c.empty: filtered_b2c = filtered_b2c[filtered_b2c['센터'].isin(expanded_main_centers)]
        if not filtered_inbound.empty: filtered_inbound = filtered_inbound[filtered_inbound['센터'].isin(expanded_main_centers)]

    total_b2c_cnt = filtered_b2c['출고건수'].sum() if not filtered_b2c.empty and '출고건수' in filtered_b2c.columns else 0
    total_inbound_cnt = filtered_inbound['입고건수'].sum() if not filtered_inbound.empty and '입고건수' in filtered_inbound.columns else 0

    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("🚚 B2C 출고건수", f"{total_b2c_cnt:,} 건")
    kpi2.metric("📦 입고건수 (입고완료/승인대기)", f"{total_inbound_cnt:,} 건")
    kpi3.metric("🏭 B2B 건수 (연동 준비중)", "0 건")
    
    st.markdown("---")
    st.subheader("📋 센터별 운영 항목 종합 비교표")
    
    summary_rows = []
    centers_to_loop = expanded_main_centers if expanded_main_centers else sorted_centers
        
    for center_name in centers_to_loop:
        b2c_c = filtered_b2c[filtered_b2c['센터'] == center_name]['출고건수'].sum() if not filtered_b2c.empty and '출고건수' in filtered_b2c.columns else 0
        in_c = filtered_inbound[filtered_inbound['센터'] == center_name]['입고건수'].sum() if not filtered_inbound.empty and '입고건수' in filtered_inbound.columns else 0
        summary_rows.append({'센터': center_name, 'B2C 출고건수': b2c_c, '입고건수 (입고완료/승인대기)': in_c, 'B2B 건수': 0})
        
    if summary_rows:
        df_summary = pd.DataFrame(summary_rows)
        pivot_main = df_summary.set_index('센터')
        
        subtotal_dfs = []
        c_375 = [c for c in pivot_main.index if '1층' in str(c) or '375 1' in str(c)]
        if c_375:
            df_375 = pivot_main.loc[pivot_main.index.isin(c_375)]
            subtotal_dfs.append(df_375)
            sum_375 = df_375.sum(axis=0)
            subtotal_dfs.append(pd.DataFrame([sum_375.values], columns=pivot_main.columns, index=pd.Index(["375 소계"], name="센터")))

        c_xfc = [c for c in pivot_main.index if 'XFC' in str(c).upper()]
        if c_xfc:
            df_xfc = pivot_main.loc[pivot_main.index.isin(c_xfc)]
            subtotal_dfs.append(df_xfc)
            sum_xfc = df_xfc.sum(axis=0)
            subtotal_dfs.append(pd.DataFrame([sum_xfc.values], columns=pivot_main.columns, index=pd.Index(["XFC 소계"], name="센터")))

        c_other = [c for c in pivot_main.index if c not in c_375 and c not in c_xfc]
        if c_other: subtotal_dfs.append(pivot_main.loc[pivot_main.index.isin(c_other)])

        body_df = pd.concat(subtotal_dfs) if subtotal_dfs else pivot_main
        total_series = pivot_main.sum(axis=0)
        total_df = pd.DataFrame([total_series.values], columns=pivot_main.columns, index=pd.Index(["★ 전체 합계"], name="센터"))

        final_main_df = pd.concat([total_df, body_df])
        render_sticky_pivot(final_main_df, ["센터"], key_suffix="main_summary")

elif main_mode == "🚚 B2C 출고 현황":
    tab1, tab2, tab3, tab4 = st.tabs(["📊 센터/고객사별 출고현황", "🚚 배송속성 / 판매처별 현황", "📦 출고박스별 현황", "🔍 SKU별 출고량"])

    with tab1:
        st.header("센터 & 고객사별 출고현황 (06시 영업마감 기준)")
        expanded_months_tab1 = render_month_button_bar(session_key_selected="tab1_exp_months", title_label="🗓️ 상세 일자 펼침 월 선택:", allow_nujak=False, multi_select=True)
        
        # 월별 능동 DB 로딩
        sel_m_str = expanded_months_tab1[0] if isinstance(expanded_months_tab1, list) and expanded_months_tab1 else "10월"
        df_b2c_orders = load_shipment_orders(sel_m_str)

        if not df_b2c_orders.empty:
            raw_centers = sorted(list(df_b2c_orders['센터'].dropna().unique()))
            center_options = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers): center_options.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers): center_options.append("XFC 소계")
            center_options.extend(raw_centers)

            col1, col2, col3 = st.columns([3, 2, 2])
            with col1:
                selected_center_input = st.multiselect("센터 선택 (미선택 시 전체)", center_options, key="tab1_centers")
                expanded_centers = expand_selected_centers(selected_center_input, raw_centers) if selected_center_input else []
            with col2: show_client = st.radio("고객사 구분 표시", ["숨김 (센터별 요약)", "보이기 (고객사 상세)"], key="tab1_show_client")
            with col3:
                if "보이기" in show_client:
                    available_clients_df = df_b2c_orders[df_b2c_orders['센터'].isin(expanded_centers)] if expanded_centers else df_b2c_orders
                    available_clients = sorted(list(available_clients_df['고객사'].dropna().unique()))
                    clients = st.multiselect("고객사 선택 (미선택 시 전체)", available_clients, key="tab1_clients")
                else: clients = []

            filtered_df = df_b2c_orders.copy()
            if expanded_centers: filtered_df = filtered_df[filtered_df['센터'].isin(expanded_centers)]
            if "보이기" in show_client and clients: filtered_df = filtered_df[filtered_df['고객사'].isin(clients)]

            group_cols = ['센터']
            if "보이기" in show_client: group_cols.append('고객사')

            if not filtered_df.empty:
                pivot_raw = pd.pivot_table(filtered_df, index=group_cols, columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_raw['총 출고건수'] = pivot_raw.sum(axis=1)
                pivot_df = smart_fold_pivot_columns(pivot_raw, expanded_months_tab1)

                render_sticky_pivot(pivot_df, group_cols, key_suffix="tab1")
        else:
            st.info("B2C 출고 데이터가 업로드되지 않았습니다. 좌측 상단 [🔄 드라이브 & 구글시트 동기화]를 눌러주세요.")

    with tab2:
        st.header("배송 속성 및 판매처별 출고현황")
        expanded_months_tab2 = render_month_button_bar(session_key_selected="tab2_exp_months", title_label="🗓️ 상세 일자 펼침 월 선택:", allow_nujak=False, multi_select=True)
        sel_m_str2 = expanded_months_tab2[0] if isinstance(expanded_months_tab2, list) and expanded_months_tab2 else "10월"
        df_b2c_orders2 = load_shipment_orders(sel_m_str2)

        if not df_b2c_orders2.empty:
            raw_centers_tab2 = sorted(list(df_b2c_orders2['센터'].dropna().unique()))
            col_t2_1, col_t2_2 = st.columns([3, 2])
            with col_t2_1:
                selected_center_tab2_input = st.multiselect("센터 선택 (미선택 시 전체)", raw_centers_tab2, key="tab2_centers")
                expanded_centers_tab2 = expand_selected_centers(selected_center_tab2_input, raw_centers_tab2) if selected_center_tab2_input else []
            with col_t2_2:
                analysis_type = st.radio("분석 기준 선택", ["배송 속성별", "판매처별"], horizontal=True, key="tab2_analysis_radio")

            target_col = '배송속성' if analysis_type == "배송 속성별" else '판매처'
            df_tab2 = df_b2c_orders2.copy()
            if expanded_centers_tab2: df_tab2 = df_tab2[df_tab2['센터'].isin(expanded_centers_tab2)]

            group_cols_tab2 = ['센터', target_col]
            if not df_tab2.empty:
                pivot_raw2 = pd.pivot_table(df_tab2, index=group_cols_tab2, columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_raw2['총 출고건수'] = pivot_raw2.sum(axis=1)
                pivot_df2 = smart_fold_pivot_columns(pivot_raw2, expanded_months_tab2)
                render_sticky_pivot(pivot_df2, group_cols_tab2, key_suffix="tab2")

    with tab3:
        st.header("출고박스 규격별 사용 현황")
        expanded_months_tab3 = render_month_button_bar(session_key_selected="tab3_exp_months", title_label="🗓️ 상세 일자 펼침 월 선택:", allow_nujak=False, multi_select=True)
        sel_m_str3 = expanded_months_tab3[0] if isinstance(expanded_months_tab3, list) and expanded_months_tab3 else "10월"
        df_b2c_orders3 = load_shipment_orders(sel_m_str3)

        if not df_b2c_orders3.empty:
            df_tab3 = df_b2c_orders3[~df_b2c_orders3['출고박스종류'].astype(str).str.upper().isin(['N', 'Y', '미지정', 'NAN'])]
            if not df_tab3.empty:
                pivot_raw3 = pd.pivot_table(df_tab3, index=['출고박스종류'], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_raw3['총 출고건수'] = pivot_raw3.sum(axis=1)
                pivot_df3 = smart_fold_pivot_columns(pivot_raw3, expanded_months_tab3)
                render_sticky_pivot(pivot_df3, ["출고박스 규격"], key_suffix="tab3")

    with tab4:
        df_b2c_sku = load_b2c_sku_data("10월")
        if not df_b2c_sku.empty:
            sku_summary = df_b2c_sku.groupby(['고객사', '바코드', 'SKU명'])[['출고건수', '총출고수량']].sum().reset_index()
            sku_summary = sku_summary.sort_values(by='총출고수량', ascending=False).reset_index(drop=True)
            st.dataframe(sku_summary.style.format({'출고건수': '{:,}', '총출고수량': '{:,}'}), use_container_width=True, hide_index=True)

elif main_mode == "📦 입고 현황":
    df_inbound = load_inbound_data()
    st.header("📦 입고 현황 모니터링")
    if not df_inbound.empty:
        pivot_raw_ib = pd.pivot_table(df_inbound, index=['센터', '고객사', '상태'], columns='영업마감일자', values='입고완료수량', aggfunc='sum', fill_value=0)
        pivot_raw_ib['총 입고완료수량'] = pivot_raw_ib.sum(axis=1)
        render_sticky_pivot(pivot_raw_ib, ['센터', '고객사', '상태'], key_suffix="inbound_main")
    else:
        st.info("입고 데이터가 존재하지 않습니다.")
