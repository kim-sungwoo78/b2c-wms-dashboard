import os
import io
import math
import calendar
import sqlite3
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timedelta, date

# 페이지 기본 설정
st.set_page_config(page_title="통합 물류 운영 대시보드", layout="wide", initial_sidebar_state="expanded")

# 분리 DB 경로 설정
DB_B2C_PATH = "wms_b2c.db"
DB_INBOUND_PATH = "wms_inbound.db"
DB_B2B_PATH = "wms_b2b.db"

def init_local_db():
    try:
        conn = sqlite3.connect(DB_B2C_PATH, timeout=10)
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
        conn.commit()
        conn.close()

        conn_ib = sqlite3.connect(DB_INBOUND_PATH, timeout=10)
        conn_ib.execute("""
        CREATE TABLE IF NOT EXISTS inbound_summary (
            영업마감일자 TEXT, 센터 TEXT, 고객사 TEXT, 상태 TEXT, 입고번호 TEXT,
            입고방법 TEXT, SKU명 TEXT, 바코드 TEXT, 소비기한 TEXT, 로트 TEXT,
            기본로케이션 TEXT, 예정수량 INTEGER, 요청SKU수량 INTEGER, 총예정수량 INTEGER,
            총검수완료수량 INTEGER, PLT수 REAL, BOX수 REAL, 파적BOX수 REAL, 등록일시 TEXT, 변경자 TEXT,
            최종변경일시 TEXT, 입고완료일시 TEXT,
            PRIMARY KEY (영업마감일자, 센터, 고객사, 상태, 입고번호, SKU명, 바코드)
        )
        """)
        conn_ib.commit()
        conn_ib.close()
    except Exception as e:
        print(f"DB Init Warning: {e}")

init_local_db()

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
                if err_msg:
                    st.session_state['sync_msg'] = f"⚠️ 동기화 완료 (시트 상태: {err_msg})"
                    st.session_state['sync_msg_type'] = "warning"
                else:
                    st.session_state['sync_msg'] = "✅ 동기화 완료! (매칭할 신규 시트 수치 없음)"
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

@st.cache_data(ttl=5)
def load_shipment_orders():
    if not os.path.exists(DB_B2C_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_B2C_PATH, timeout=10)
        df_raw = pd.read_sql("""
            SELECT 영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, COUNT(DISTINCT 송장번호) AS 출고건수
            FROM shipment_raw
            GROUP BY 영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류
        """, conn)
        
        if not df_raw.empty:
            conn.close()
            return df_raw
            
        df_daily = pd.read_sql("""
            SELECT 영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류, SUM(출고건수) AS 출고건수
            FROM daily_summary
            GROUP BY 영업마감일자, 센터, 고객사, 배송속성, 판매처, 출고박스종류
        """, conn)
        conn.close()
        return df_daily
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=5)
def load_b2c_sku_data():
    if not os.path.exists(DB_B2C_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_B2C_PATH, timeout=10)
        df = pd.read_sql("SELECT * FROM daily_summary", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=5)
def load_inbound_data():
    if not os.path.exists(DB_INBOUND_PATH):
        return pd.DataFrame()
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
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=5)
def load_inbound_distinct_counts():
    if not os.path.exists(DB_INBOUND_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_INBOUND_PATH, timeout=10)
        df = pd.read_sql("""
            SELECT 영업마감일자, 센터, 고객사,
                   COUNT(DISTINCT 입고번호) AS 입고건수
            FROM inbound_summary
            WHERE 상태 LIKE '%입고완료%' OR 상태 LIKE '%입고 완료%' OR 상태 LIKE '%승인대기%' OR 상태 LIKE '%승인 대기%'
            GROUP BY 영업마감일자, 센터, 고객사
        """, conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

# CSS 스티키 테이블 스타일
st.markdown("""
<style>
    .sticky-table-container {
        max-height: 750px;
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
        padding: 9px 13px;
        text-align: center !important;
        border-bottom: 1px solid #1f2937;
        border-right: 1px solid #1f2937;
        white-space: nowrap;
        background-color: #0e1117;
    }
    .sticky-table thead tr th {
        position: sticky; top: 0; z-index: 20;
        background-color: #1f2937 !important; color: #9ca3af; font-weight: bold; text-align: center !important;
    }
    .sticky-table tr.total-row td {
        position: sticky; top: 35px; z-index: 15;
        background-color: #1e293b !important; color: #facc15 !important;
        font-weight: bold; border-bottom: 2px solid #eab308 !important; text-align: center !important;
    }
    .sticky-table th.freeze-col-1, .sticky-table td.freeze-col-1 {
        position: sticky; left: 0; z-index: 10;
        background-color: #111827 !important; border-right: 1px solid #374151 !important; text-align: center !important;
    }
    .sticky-table th.freeze-col-2, .sticky-table td.freeze-col-2 {
        position: sticky; left: 140px; z-index: 10;
        background-color: #111827 !important; border-right: 1px solid #374151 !important; text-align: center !important;
    }
    .sticky-table tr.subtotal-row td {
        background-color: #0f172a !important; color: #38bdf8 !important; font-weight: bold; text-align: center !important;
    }
    .sticky-table .month-sum-col {
        background-color: #172554 !important; color: #60a5fa !important;
        font-weight: bold !important; border-right: 2px solid #2563eb !important; border-left: 2px solid #2563eb !important;
        text-align: center !important;
    }
</style>
""", unsafe_allow_html=True)

st.title("🏢 센터 통합 물류 운영 대시보드")

if st.sidebar.button("🔄 드라이브 & 구글시트 동기화"):
    if run_sync():
        st.rerun()

if 'sync_msg' in st.session_state and st.session_state['sync_msg']:
    m_type = st.session_state.get('sync_msg_type', 'info')
    if m_type == "success":
        st.sidebar.success(st.session_state['sync_msg'])
    elif m_type == "warning":
        st.sidebar.warning(st.session_state['sync_msg'])
    elif m_type == "error":
        st.sidebar.error(st.session_state['sync_msg'])
    else:
        st.sidebar.info(st.session_state['sync_msg'])

df_b2c_orders = load_shipment_orders()
df_b2c_sku = load_b2c_sku_data()
df_inbound = load_inbound_data()
df_inbound_distinct = load_inbound_distinct_counts()

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

# 팝업 대형 모달 다이얼로그 (그래프 전용)
@st.dialog("📈 입고 종합 추세 그래프 전체 확대 보기", width="large")
def show_popup_chart_modal(df_ib_filtered, chart_start_date, chart_end_date):
    render_inbound_interactive_plotly_chart(df_ib_filtered, chart_start_date=chart_start_date, chart_end_date=chart_end_date, height=650)

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
    for tc in total_cols:
        new_df[tc] = pivot_df[tc]

    for m_key, info in sorted(month_groups.items()):
        m_label = info['label']
        m_sum_col_name = f"{m_key[5:7]}월 합계"
        m_dates = info['dates']
        
        new_df[m_sum_col_name] = pivot_df[m_dates].sum(axis=1)
        
        if m_label in expanded_months or f"{int(m_key[5:7]):02d}월" in expanded_months or m_key in expanded_months:
            for d in m_dates:
                new_df[d] = pivot_df[d]
                
    return new_df

def render_month_button_bar(df_target, session_key_selected, title_label="🗓️ 상세 일자 펼침 월 선택 (미선택 시 월합계만 접힘):", allow_nujak=False, multi_select=True):
    st.markdown(f"<p style='font-size:14px; font-weight:bold; margin-bottom:5px;'>{title_label}</p>", unsafe_allow_html=True)
    
    active_months_set = set()
    if not df_target.empty and '영업마감일자' in df_target.columns:
        active_months_set = set(df_target['영업마감일자'].dropna().str.slice(5, 7).apply(lambda x: f"{int(x)}월"))

    adjusted_today = datetime.now() - timedelta(days=1)
    default_month_str = f"{adjusted_today.month}월"

    if session_key_selected not in st.session_state:
        if multi_select:
            st.session_state[session_key_selected] = [default_month_str] if default_month_str in active_months_set else []
        else:
            st.session_state[session_key_selected] = default_month_str if default_month_str in active_months_set else "누적"

    months_list = [f"{i}월" for i in range(1, 13)]
    if allow_nujak:
        months_list.append("누적")

    num_cols = len(months_list)
    cols = st.columns(num_cols)

    for idx, m_name in enumerate(months_list):
        with cols[idx]:
            is_active_data = (m_name in active_months_set) or (m_name == "누적")
            
            if multi_select:
                is_selected = m_name in st.session_state[session_key_selected]
            else:
                is_selected = (st.session_state[session_key_selected] == m_name)
                
            btn_style = "primary" if is_selected else "secondary"
            
            if is_active_data:
                if st.button(m_name, key=f"btn_{session_key_selected}_{m_name}", type=btn_style, use_container_width=True):
                    if multi_select:
                        curr_list = list(st.session_state[session_key_selected])
                        if m_name in curr_list:
                            curr_list.remove(m_name)
                        else:
                            curr_list.append(m_name)
                        st.session_state[session_key_selected] = curr_list
                    else:
                        st.session_state[session_key_selected] = m_name
                    st.rerun()
            else:
                st.button(m_name, key=f"btn_disabled_{session_key_selected}_{m_name}", disabled=True, use_container_width=True)

    return st.session_state[session_key_selected]

def generate_pure_svg_donut(data_dict, title):
    colors = ['#38bdf8', '#60a5fa', '#facc15', '#4ade80', '#f43f5e', '#a855f7']
    total_val = sum(data_dict.values())
    
    if not data_dict or total_val == 0:
        return f"""
        <div style="background-color:#0e1117; border:1px solid #1f2937; border-radius:8px; padding:15px; text-align:center;">
            <div style="font-size:12px; font-weight:bold; color:#f3f4f6; margin-bottom:10px;">{title}</div>
            <div style="padding:30px; color:#9ca3af; font-size:12px;">데이터 없음</div>
        </div>
        """
        
    cx, cy, r = 90, 90, 60
    stroke_width = 25
    circumference = 2 * math.pi * r
    
    svg_parts = [
        f'<svg width="180" height="180" viewBox="0 0 180 180" style="display:block; margin:auto;">',
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#1f2937" stroke-width="{stroke_width}"/>'
    ]
    
    cumulative_pct = 0
    legend_parts = ['<div style="font-size:11px; margin-top:10px; text-align:left;">']
    
    for idx, (label, val) in enumerate(data_dict.items()):
        if val <= 0:
            continue
        pct = val / total_val
        color = colors[idx % len(colors)]
        
        dash_array = f"{pct * circumference:.2f} {circumference:.2f}"
        dash_offset = f"{-cumulative_pct * circumference:.2f}"
        
        svg_parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke_width}" stroke-dasharray="{dash_array}" '
            f'stroke-dashoffset="{dash_offset}" transform="rotate(-90 {cx} {cy})"/>'
        )
        cumulative_pct += pct
        
        legend_parts.append(
            f'<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">'
            f'<span style="color:#9ca3af;"><span style="display:inline-block; width:8px; height:8px; background-color:{color}; border-radius:50%; margin-right:5px;"></span>'
            f'{label}</span><span style="font-weight:bold; color:#e5e7eb;">{pct*100:.1f}% ({val:,})</span></div>'
        )
        
    legend_parts.append('</div>')
    
    svg_parts.append(f'<text x="{cx}" y="{cy-5}" text-anchor="middle" fill="#9ca3af" font-size="10" font-weight="bold">{title}</text>')
    svg_parts.append(f'<text x="{cx}" y="{cy+12}" text-anchor="middle" fill="#facc15" font-size="12" font-weight="bold">{total_val:,}</text>')
    svg_parts.append('</svg>')
    
    return f'<div style="background-color:#0e1117; border:1px solid #1f2937; border-radius:8px; padding:12px; text-align:center;">' + "".join(svg_parts) + "".join(legend_parts) + '</div>'

# ★ [Plotly.js 기반 깔끔 정렬 범례 토글 차트 - 자동 리사이즈 및 가로 수평 범례 배치] ★
def render_inbound_interactive_plotly_chart(df_ib_filtered, chart_start_date=None, chart_end_date=None, height=480):
    if df_ib_filtered.empty or '영업마감일자' not in df_ib_filtered.columns:
        return

    daily_chart_df = df_ib_filtered.groupby('영업마감일자')[
        ['입고건수', 'SKU개수', '입고완료수량', 'PLT수', 'BOX수']
    ].sum().reset_index()

    if daily_chart_df.empty:
        return

    if chart_start_date and chart_end_date:
        daily_chart_df['dt'] = pd.to_datetime(daily_chart_df['영업마감일자'], errors='coerce').dt.date
        daily_chart_df = daily_chart_df[(daily_chart_df['dt'] >= chart_start_date) & (daily_chart_df['dt'] <= chart_end_date)]

    daily_chart_df = daily_chart_df.sort_values(by='영업마감일자').reset_index(drop=True)

    dates = list(daily_chart_df['영업마감일자'])
    inbound_cnt = [int(v) for v in daily_chart_df['입고건수']]
    sku_cnt = [int(v) for v in daily_chart_df['SKU개수']]
    completed_ea = [int(v) for v in daily_chart_df['입고완료수량']]
    plt_cnt = [float(v) for v in daily_chart_df['PLT수']]
    box_cnt = [float(v) for v in daily_chart_df['BOX수']]

    html_code = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
        <style>
            body {{ margin: 0; padding: 0; background-color: #0e1117; font-family: sans-serif; overflow: hidden; }}
            #plotly_div {{ width: 100%; height: {height}px; }}
        </style>
    </head>
    <body>
        <div id="plotly_div"></div>
        <script>
            var dates = {dates};
            var trace_bar = {{
                x: dates, y: {inbound_cnt}, name: '입고건수 (건)', type: 'bar', marker: {{color: '#38bdf8', opacity: 0.75}}
            }};
            var trace_sku = {{
                x: dates, y: {sku_cnt}, name: 'SKU개수 (종)', type: 'scatter', mode: 'lines+markers', line: {{color: '#facc15', width: 2.5}}
            }};
            var trace_plt = {{
                x: dates, y: {plt_cnt}, name: 'PLT수 (PLT)', type: 'scatter', mode: 'lines+markers', line: {{color: '#4ade80', width: 2.5}}
            }};
            var trace_box = {{
                x: dates, y: {box_cnt}, name: 'BOX수 (BOX)', type: 'scatter', mode: 'lines+markers', line: {{color: '#a855f7', width: 2.5}}
            }};
            var trace_ea = {{
                x: dates, y: {completed_ea}, name: '입고완료수량 (EA)', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: {{color: '#f43f5e', width: 2, dash: 'dot'}}
            }};

            var data = [trace_bar, trace_sku, trace_plt, trace_box, trace_ea];

            var layout = {{
                paper_bgcolor: '#0e1117',
                plot_bgcolor: '#0e1117',
                margin: {{l: 50, r: 70, t: 30, b: 50}},
                xaxis: {{type: 'category', tickfont: {{color: '#9ca3af', size: 11}}, gridcolor: '#1f2937'}},
                yaxis: {{title: '건수 / 종수 / PLT / BOX', titlefont: {{color: '#9ca3af', size: 12}}, tickfont: {{color: '#9ca3af', size: 11}}, gridcolor: '#1f2937'}},
                yaxis2: {{title: '입고완료수량 (EA)', titlefont: {{color: '#f43f5e', size: 12}}, tickfont: {{color: '#f43f5e', size: 11}}, overlaying: 'y', side: 'right', showgrid: false}},
                legend: {{
                    orientation: 'h',
                    xanchor: 'left',
                    x: 0,
                    y: 1.12,
                    font: {{color: '#e5e7eb', size: 11}},
                    itemwidth: 30
                }},
                hovermode: 'x unified'
            }};

            Plotly.newPlot('plotly_div', data, layout, {{responsive: true, displayModeBar: false}});

            // 탭 전환 시 너비 재계산 수평 정렬 보장
            window.addEventListener('resize', function() {{ Plotly.Plots.resize('plotly_div'); }});
            setTimeout(function() {{ Plotly.Plots.resize('plotly_div'); }}, 150);
            setTimeout(function() {{ Plotly.Plots.resize('plotly_div'); }}, 400);
        </script>
    </body>
    </html>
    """
    components.html(html_code, height=height + 20)

# 메인 종합 현황 모드
if main_mode == "🏢 메인 : 센터 종합 현황":
    st.header("📊 센터 종합 운영 실적 요약")
    
    raw_centers = set()
    if not df_b2c_orders.empty and '센터' in df_b2c_orders.columns:
        raw_centers.update(df_b2c_orders['센터'].dropna().unique())
    if not df_inbound.empty and '센터' in df_inbound.columns:
        raw_centers.update(df_inbound['센터'].dropna().unique())
        
    sorted_centers = sorted(list(raw_centers))
    
    center_options_main = []
    if any('1층' in str(c) or '375 1' in str(c) for c in sorted_centers):
        center_options_main.append("375 소계")
    if any('XFC' in str(c).upper() for c in sorted_centers):
        center_options_main.append("XFC 소계")
    center_options_main.extend(sorted_centers)

    selected_centers_filter = st.multiselect(
        "🏢 센터 선택 (미선택 시 전체):", 
        center_options_main, 
        default=[],
        key="main_center_filter"
    )
    expanded_main_centers = expand_selected_centers(selected_centers_filter, sorted_centers) if selected_centers_filter else []

    df_combined_target = pd.concat([df_b2c_orders, df_inbound_distinct]) if not df_b2c_orders.empty else df_inbound_distinct

    selected_m = render_month_button_bar(
        df_combined_target, 
        session_key_selected="main_selected_month", 
        title_label="🗓️ 기준 월 선택:", 
        allow_nujak=True, 
        multi_select=False
    )

    filtered_b2c = df_b2c_orders.copy() if not df_b2c_orders.empty else pd.DataFrame()
    filtered_inbound = df_inbound_distinct.copy() if not df_inbound_distinct.empty else pd.DataFrame()

    if expanded_main_centers:
        if not filtered_b2c.empty and '센터' in filtered_b2c.columns:
            filtered_b2c = filtered_b2c[filtered_b2c['센터'].isin(expanded_main_centers)]
        if not filtered_inbound.empty and '센터' in filtered_inbound.columns:
            filtered_inbound = filtered_inbound[filtered_inbound['센터'].isin(expanded_main_centers)]

    if selected_m != "누적":
        m_digit = selected_m.replace("월", "").zfill(2)
        if not filtered_b2c.empty and '영업마감일자' in filtered_b2c.columns:
            filtered_b2c = filtered_b2c[filtered_b2c['영업마감일자'].str.slice(5, 7) == m_digit]
        if not filtered_inbound.empty and '영업마감일자' in filtered_inbound.columns:
            filtered_inbound = filtered_inbound[filtered_inbound['영업마감일자'].str.slice(5, 7) == m_digit]

    total_b2c_cnt = filtered_b2c['출고건수'].sum() if not filtered_b2c.empty and '출고건수' in filtered_b2c.columns else 0
    total_inbound_cnt = filtered_inbound['입고건수'].sum() if not filtered_inbound.empty and '입고건수' in filtered_inbound.columns else 0
    total_b2b_cnt = 0

    st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("🚚 B2C 출고건수", f"{total_b2c_cnt:,} 건")
    kpi2.metric("📦 입고건수 (입고완료/승인대기)", f"{total_inbound_cnt:,} 건")
    kpi3.metric("🏭 B2B 건수 (연동 준비중)", f"{total_b2b_cnt:,} 건")
    
    st.markdown("---")
    st.subheader("📋 센터별 운영 항목 종합 비교표")
    
    summary_rows = []
    centers_to_loop = expanded_main_centers if expanded_main_centers else sorted_centers
        
    for center_name in centers_to_loop:
        b2c_c = filtered_b2c[filtered_b2c['센터'] == center_name]['출고건수'].sum() if not filtered_b2c.empty and '출고건수' in filtered_b2c.columns else 0
        in_c = filtered_inbound[filtered_inbound['센터'] == center_name]['입고건수'].sum() if not filtered_inbound.empty and '입고건수' in filtered_inbound.columns else 0
        b2b_c = 0
        
        summary_rows.append({
            '센터': center_name,
            'B2C 출고건수': b2c_c,
            '입고건수 (입고완료/승인대기)': in_c,
            'B2B 건수': b2b_c
        })
        
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
        if c_other:
            df_other = pivot_main.loc[pivot_main.index.isin(c_other)]
            subtotal_dfs.append(df_other)

        body_df = pd.concat(subtotal_dfs) if subtotal_dfs else pivot_main
        total_series = pivot_main.sum(axis=0)
        total_df = pd.DataFrame([total_series.values], columns=pivot_main.columns, index=pd.Index(["★ 전체 합계"], name="센터"))

        final_main_df = pd.concat([total_df, body_df])
        render_sticky_pivot(final_main_df, ["센터"], key_suffix="main_summary")

        st.markdown("---")
        st.subheader("🥧 센터별 실적 항목 비율 (원형 도넛 그래프)")
        
        chart_df = df_summary[~df_summary['센터'].str.contains('소계|합계', na=False)]
        
        b2c_dict = dict(zip(chart_df['센터'], chart_df['B2C 출고건수']))
        in_dict = dict(zip(chart_df['센터'], chart_df['입고건수 (입고완료/승인대기)']))
        b2b_dict = dict(zip(chart_df['센터'], chart_df['B2B 건수']))

        ch_col1, ch_col2, ch_col3 = st.columns(3)
        
        with ch_col1:
            st.markdown(generate_pure_svg_donut(b2c_dict, "🚚 B2C 출고건수"), unsafe_allow_html=True)

        with ch_col2:
            st.markdown(generate_pure_svg_donut(in_dict, "📦 입고건수"), unsafe_allow_html=True)

        with ch_col3:
            st.markdown(generate_pure_svg_donut(b2b_dict, "🏭 B2B 건수"), unsafe_allow_html=True)

    else:
        st.info("데이터가 준비되어 있지 않습니다. 좌측 상단 [🔄 드라이브 & 구글시트 동기화]를 눌러 동기화를 진행해주세요.")

elif main_mode == "🚚 B2C 출고 현황":
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 센터/고객사별 출고현황", 
        "🚚 배송속성 / 판매처별 현황", 
        "📦 출고박스별 현황",
        "🔍 SKU별 출고량"
    ])

    with tab1:
        st.header("센터 & 고객사별 출고현황 (06시 영업마감 기준)")
        if not df_b2c_orders.empty:
            raw_centers = sorted(list(df_b2c_orders['센터'].dropna().unique()))
            center_options = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers):
                center_options.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers):
                center_options.append("XFC 소계")
            center_options.extend(raw_centers)

            col1, col2, col3 = st.columns([3, 2, 2])
            with col1:
                selected_center_input = st.multiselect("센터 선택 (미선택 시 전체)", center_options, key="tab1_centers")
                expanded_centers = expand_selected_centers(selected_center_input, raw_centers) if selected_center_input else []
            with col2:
                show_client = st.radio("고객사 구분 표시", ["숨김 (센터별 요약)", "보이기 (고객사 상세)"])
            with col3:
                if "보이기" in show_client:
                    available_clients_df = df_b2c_orders[df_b2c_orders['센터'].isin(expanded_centers)] if expanded_centers else df_b2c_orders
                    available_clients = sorted(list(available_clients_df['고객사'].dropna().unique()))
                    clients = st.multiselect("고객사 선택 (미선택 시 전체)", available_clients, key="tab1_clients")
                else:
                    clients = []
                    st.selectbox("고객사 선택", ["고객사 숨김 상태"], disabled=True)

            expanded_months_tab1 = render_month_button_bar(
                df_b2c_orders, 
                session_key_selected="tab1_exp_months", 
                title_label="🗓️ 상세 일자 펼침 월 선택 (미선택 시 월합계만 접힘):", 
                allow_nujak=False, 
                multi_select=True
            )

            filtered_df = df_b2c_orders.copy()
            if expanded_centers: 
                filtered_df = filtered_df[filtered_df['센터'].isin(expanded_centers)]
            if "보이기" in show_client and clients: 
                filtered_df = filtered_df[filtered_df['고객사'].isin(clients)]

            group_cols = ['센터']
            if "보이기" in show_client: 
                group_cols.append('고객사')

            if not filtered_df.empty:
                pivot_raw = pd.pivot_table(filtered_df, index=group_cols, columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_raw['총 출고건수'] = pivot_raw.sum(axis=1)
                
                pivot_df = smart_fold_pivot_columns(pivot_raw, expanded_months_tab1)

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
                total_label = "★ 전체 합계"
                total_idx = pd.MultiIndex.from_tuples([(total_label, "전체")], names=group_cols) if "보이기" in show_client else pd.Index([total_label], name="센터")
                total_df = pd.DataFrame([total_series.values], columns=pivot_df.columns, index=total_idx)

                final_df = pd.concat([total_df, body_df])
                render_sticky_pivot(final_df, group_cols, key_suffix="tab1")
        else:
            st.info("B2C 출고 데이터가 업로드되지 않았습니다. 좌측 상단 [🔄 드라이브 & 구글시트 동기화]를 눌러주세요.")

    with tab2:
        st.header("배송 속성 및 판매처별 출고현황")
        if not df_b2c_orders.empty:
            col_t1, _ = st.columns([3, 5])
            with col_t1:
                analysis_type = st.radio("분석 기준 선택", ["배송 속성별", "판매처별"], horizontal=True)

            expanded_months_tab2 = render_month_button_bar(
                df_b2c_orders, 
                session_key_selected="tab2_exp_months", 
                title_label="🗓️ 상세 일자 펼침 월 선택 (미선택 시 월합계만 접힘):", 
                allow_nujak=False, 
                multi_select=True
            )

            target_col = '배송속성' if analysis_type == "배송 속성별" else '판매처'
            df_tab2 = df_b2c_orders.copy()

            pivot_raw2 = pd.pivot_table(df_tab2, index=[target_col], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
            pivot_raw2['총 출고건수'] = pivot_raw2.sum(axis=1)
            
            pivot_df2 = smart_fold_pivot_columns(pivot_raw2, expanded_months_tab2)

            total_series2 = pivot_df2.sum(axis=0)
            total_label2 = "★ 전체 합계"
            total_df2 = pd.DataFrame([total_series2.values], columns=pivot_df2.columns, index=pd.Index([total_label2], name=target_col))
            final_df2 = pd.concat([total_df2, pivot_df2])
            render_sticky_pivot(final_df2, [target_col], key_suffix="tab2")

    with tab3:
        st.header("출고박스 규격별 사용 현황")
        if not df_b2c_orders.empty:
            raw_centers_tab3 = sorted(list(df_b2c_orders['센터'].dropna().unique()))
            center_options_tab3 = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers_tab3):
                center_options_tab3.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers_tab3):
                center_options_tab3.append("XFC 소계")
            center_options_tab3.extend(raw_centers_tab3)

            box_col1, box_col2 = st.columns([3, 3])
            with box_col1:
                selected_centers_input_tab3 = st.multiselect("센터 선택 (미선택 시 전체)", center_options_tab3, key="tab3_centers")
                expanded_centers_tab3 = expand_selected_centers(selected_centers_input_tab3, raw_centers_tab3) if selected_centers_input_tab3 else []
            
            filtered_by_center_tab3 = df_b2c_orders[df_b2c_orders['센터'].isin(expanded_centers_tab3)] if expanded_centers_tab3 else df_b2c_orders
            available_clients_tab3 = sorted(list(filtered_by_center_tab3['고객사'].dropna().unique()))
            
            with box_col2:
                selected_clients_tab3 = st.multiselect("고객사 선택 (미선택 시 전체)", available_clients_tab3, key="tab3_clients")

            expanded_months_tab3 = render_month_button_bar(
                df_b2c_orders, 
                session_key_selected="tab3_exp_months", 
                title_label="🗓️ 상세 일자 펼침 월 선택 (미선택 시 월합계만 접힘):", 
                allow_nujak=False, 
                multi_select=True
            )

            df_tab3 = df_b2c_orders.copy()
            if expanded_centers_tab3:
                df_tab3 = df_tab3[df_tab3['센터'].isin(expanded_centers_tab3)]
            if selected_clients_tab3:
                df_tab3 = df_tab3[df_tab3['고객사'].isin(selected_clients_tab3)]

            df_tab3 = df_tab3[~df_tab3['출고박스종류'].astype(str).str.upper().isin(['N', 'Y', '미지정', 'NAN'])]

            if not df_tab3.empty:
                pivot_raw3 = pd.pivot_table(df_tab3, index=['출고박스종류'], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
                pivot_raw3['총 출고건수'] = pivot_raw3.sum(axis=1)
                
                pivot_df3 = smart_fold_pivot_columns(pivot_raw3, expanded_months_tab3)

                total_series3 = pivot_df3.sum(axis=0)
                total_label3 = "★ 전체 합계"
                total_df3 = pd.DataFrame([total_series3.values], columns=pivot_df3.columns, index=pd.Index([total_label3], name="출고박스 규격"))
                final_df3 = pd.concat([total_df3, pivot_df3])
                render_sticky_pivot(final_df3, ["출고박스 규격"], key_suffix="tab3")
            else:
                st.info("선택한 센터/고객사의 출고 박스 규격 데이터가 존재하지 않습니다.")

    with tab4:
        st.header("🔍 SKU별 출고량 (기간 선택 집계)")
        if not df_b2c_sku.empty:
            df_b2c_sku['영업마감일자_dt'] = pd.to_datetime(df_b2c_sku['영업마감일자'], errors='coerce')
            
            # ★ 디폴트 조회 기간: 당월 1일 ~ 어제까지 ★
            today_dt = date.today()
            yesterday_dt = today_dt - timedelta(days=1)
            default_start_dt = date(today_dt.year, today_dt.month, 1)

            min_db_dt = df_b2c_sku['영업마감일자_dt'].min().date() if not df_b2c_sku['영업마감일자_dt'].isna().all() else default_start_dt
            max_db_dt = df_b2c_sku['영업마감일자_dt'].max().date() if not df_b2c_sku['영업마감일자_dt'].isna().all() else yesterday_dt

            if 'sku_date_mode' not in st.session_state:
                st.session_state['sku_date_mode'] = "당월"
                st.session_state['sku_start_date'] = default_start_dt
                st.session_state['sku_end_date'] = yesterday_dt

            raw_centers_tab4 = sorted(list(df_b2c_sku['센터'].dropna().unique()))
            center_options_tab4 = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers_tab4):
                center_options_tab4.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers_tab4):
                center_options_tab4.append("XFC 소계")
            center_options_tab4.extend(raw_centers_tab4)

            # 상단 1행: 센터 및 고객사 선택
            col1, col2 = st.columns(2)
            with col1:
                selected_centers_input_tab4 = st.multiselect("센터 선택 (다중 선택 가능)", center_options_tab4, key="tab4_centers")
                expanded_centers_tab4 = expand_selected_centers(selected_centers_input_tab4, raw_centers_tab4) if selected_centers_input_tab4 else []
            
            filtered_by_center = df_b2c_sku[df_b2c_sku['센터'].isin(expanded_centers_tab4)] if expanded_centers_tab4 else df_b2c_sku
            available_clients = sorted(list(filtered_by_center['고객사'].dropna().unique()))
            
            with col2:
                selected_clients = st.multiselect("고객사 선택 (선택한 센터의 고객사만 표시)", available_clients, key="tab4_clients")

            st.markdown("<p style='font-size:14px; font-weight:bold; margin-top:10px; margin-bottom:5px;'>📅 조회 기간 지정 및 빠른 선택 (단일 선택):</p>", unsafe_allow_html=True)
            
            avail_months_tuples = sorted(list(set(df_b2c_sku['영업마감일자'].str.slice(0, 7).dropna().unique())), reverse=True)
            avail_months_labels = [f"{t[:4]}년 {int(t[5:7])}월" for t in avail_months_tuples]

            curr_sku_s = st.session_state['sku_start_date']
            curr_sku_e = st.session_state['sku_end_date']
            active_sku_mode = st.session_state['sku_date_mode']

            p_col1, p_col2, p_col3, p_col4, p_col5, p_col6, p_col7, p_col8 = st.columns([1.8, 1.8, 2.5, 1.0, 1.0, 1.0, 1.0, 1.0])

            with p_col1:
                input_start = st.date_input("시작일자:", value=curr_sku_s, key=f"sku_s_{curr_sku_s}_{curr_sku_e}", label_visibility="collapsed")
            with p_col2:
                input_end = st.date_input("종료일자:", value=curr_sku_e, key=f"sku_e_{curr_sku_s}_{curr_sku_e}", label_visibility="collapsed")

            if input_start != curr_sku_s or input_end != curr_sku_e:
                st.session_state['sku_date_mode'] = "직접지정"
                st.session_state['sku_start_date'] = input_start
                st.session_state['sku_end_date'] = input_end
                st.rerun()

            need_update_sku = False
            new_sku_s, new_sku_e, new_sku_mode = curr_sku_s, curr_sku_e, active_sku_mode

            def get_sku_btn_type(b_mode):
                return "primary" if active_sku_mode == b_mode else "secondary"

            with p_col3:
                selected_m_labels = st.multiselect("월 선택 (1일~말일 지정)", avail_months_labels, key="sku_m_select_widget", label_visibility="collapsed", placeholder="월 선택 (다중가능)")
                if selected_m_labels:
                    selected_years_months = []
                    for lbl in selected_m_labels:
                        parts = lbl.replace("년", "").replace("월", "").split()
                        selected_years_months.append((int(parts[0]), int(parts[1])))
                    
                    min_m_tuple = min(selected_years_months)
                    max_m_tuple = max(selected_years_months)
                    
                    m_start = date(min_m_tuple[0], min_m_tuple[1], 1)
                    _, last_d = calendar.monthrange(max_m_tuple[0], max_m_tuple[1])
                    m_end = date(max_m_tuple[0], max_m_tuple[1], last_d)
                    
                    if curr_sku_s != m_start or curr_sku_e != m_end:
                        new_sku_s, new_sku_e, new_sku_mode = m_start, m_end, "월선택"
                        need_update_sku = True

            with p_col4:
                if st.button("오늘", key="btn_sku_today", type=get_sku_btn_type("오늘"), use_container_width=True):
                    new_sku_s, new_sku_e, new_sku_mode = yesterday_dt, yesterday_dt, "오늘"
                    need_update_sku = True
            with p_col5:
                if st.button("일주일", key="btn_sku_week", type=get_sku_btn_type("일주일"), use_container_width=True):
                    new_sku_s = max(min_db_dt, yesterday_dt - timedelta(days=6))
                    new_sku_e, new_sku_mode = yesterday_dt, "일주일"
                    need_update_sku = True
            with p_col6:
                if st.button("1개월", key="btn_sku_1m", type=get_sku_btn_type("1개월"), use_container_width=True):
                    new_sku_s = max(min_db_dt, yesterday_dt - timedelta(days=30))
                    new_sku_e, new_sku_mode = yesterday_dt, "1개월"
                    need_update_sku = True
            with p_col7:
                if st.button("3개월", key="btn_sku_3m", type=get_sku_btn_type("3개월"), use_container_width=True):
                    new_sku_s = max(min_db_dt, yesterday_dt - timedelta(days=90))
                    new_sku_e, new_sku_mode = yesterday_dt, "3개월"
                    need_update_sku = True
            with p_col8:
                if st.button("전체 기간", key="btn_sku_all", type=get_sku_btn_type("전체 기간"), use_container_width=True):
                    new_sku_s, new_sku_e, new_sku_mode = min_db_dt, max_db_dt, "전체 기간"
                    need_update_sku = True

            if need_update_sku:
                st.session_state['sku_date_mode'] = new_sku_mode
                st.session_state['sku_start_date'] = new_sku_s
                st.session_state['sku_end_date'] = new_sku_e
                st.rerun()

            sku_df = df_b2c_sku.copy()
            if curr_sku_s and curr_sku_e:
                sku_df = sku_df[(sku_df['영업마감일자_dt'].dt.date >= curr_sku_s) & (sku_df['영업마감일자_dt'].dt.date <= curr_sku_e)]

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

elif main_mode == "📦 입고 현황":
    in_tab1, in_tab2 = st.tabs([
        "📊 센터/고객사별 입고 현황",
        "📋 상태별(입고완료/승인대기) 현황"
    ])

    with in_tab1:
        st.header("📦 센터 & 고객사별 입고 현황 (구글 시트 PLT / BOX 매칭)")
        if not df_inbound.empty:
            raw_centers_inbound = sorted(list(df_inbound['센터'].dropna().unique()))
            center_options_inbound = []
            if any('1층' in str(c) or '375 1' in str(c) for c in raw_centers_inbound):
                center_options_inbound.append("375 소계")
            if any('XFC' in str(c).upper() for c in raw_centers_inbound):
                center_options_inbound.append("XFC 소계")
            center_options_inbound.extend(raw_centers_inbound)

            # 1행: 센터/고객사 조건 선택 컨트롤러
            col_ib1, col_ib2, col_ib3 = st.columns([3, 2, 2])
            with col_ib1:
                selected_center_input_ib = st.multiselect("센터 선택 (미선택 시 전체)", center_options_inbound, key="inbound_tab1_centers")
                expanded_centers_ib = expand_selected_centers(selected_center_input_ib, raw_centers_inbound) if selected_center_input_ib else []
            with col_ib2:
                show_client_ib = st.radio("고객사 구분 표시", ["숨김 (센터별 요약)", "보이기 (고객사 상세)"], key="inbound_show_client")
            with col_ib3:
                if "보이기" in show_client_ib:
                    available_clients_df_ib = df_inbound[df_inbound['센터'].isin(expanded_centers_ib)] if expanded_centers_ib else df_inbound
                    available_clients_ib = sorted(list(available_clients_df_ib['고객사'].dropna().unique()))
                    clients_ib = st.multiselect("고객사 선택 (미선택 시 전체)", available_clients_ib, key="inbound_tab1_clients")
                else:
                    clients_ib = []
                    st.selectbox("고객사 선택", ["고객사 숨김 상태"], disabled=True, key="inbound_client_disabled")

            # 2행: 월 선택 가로 버튼 바 (표 전용 펼침 제어)
            expanded_months_ib1 = render_month_button_bar(
                df_inbound, 
                session_key_selected="ib1_exp_months", 
                title_label="🗓️ 상세 일자 펼침 월 선택 (미선택 시 월합계만 접힘):", 
                allow_nujak=False, 
                multi_select=True
            )

            # 3행: 조회 항목 선택
            st.markdown("<p style='font-size:14px; font-weight:bold; margin-top:10px; margin-bottom:5px;'>조회 항목 선택:</p>", unsafe_allow_html=True)
            metric_val = st.radio(
                "조회 항목 선택:", 
                ["입고건수 (건)", "SKU개수 (종)", "입고완료수량 (EA)", "PLT수 (PLT)", "BOX수 (BOX)"], 
                horizontal=True,
                index=0,
                key="inbound_metric_radio",
                label_visibility="collapsed"
            )

            col_map_dict = {
                "입고건수 (건)": "입고건수",
                "SKU개수 (종)": "SKU개수",
                "입고완료수량 (EA)": "입고완료수량",
                "PLT수 (PLT)": "PLT수",
                "BOX수 (BOX)": "BOX수"
            }
            target_val = col_map_dict[metric_val]

            filtered_df_ib = df_inbound.copy()
            if expanded_centers_ib:
                filtered_df_ib = filtered_df_ib[filtered_df_ib['센터'].isin(expanded_centers_ib)]
            if "보이기" in show_client_ib and clients_ib:
                filtered_df_ib = filtered_df_ib[filtered_df_ib['고객사'].isin(clients_ib)]

            group_cols_ib = ['센터']
            if "보이기" in show_client_ib:
                group_cols_ib.append('고객사')

            if not filtered_df_ib.empty and target_val in filtered_df_ib.columns:
                # ★ 기본 디폴트 조회 기간 설정: 당월 1일 ~ 어제까지 ★
                today_dt = date.today()
                yesterday_dt = today_dt - timedelta(days=1)
                default_start_dt = date(today_dt.year, today_dt.month, 1)

                df_ib_dt = filtered_df_ib.copy()
                df_ib_dt['dt'] = pd.to_datetime(df_ib_dt['영업마감일자'], errors='coerce')
                ib_min_date = df_ib_dt['dt'].min().date() if not df_ib_dt['dt'].isna().all() else default_start_dt
                ib_max_date = df_ib_dt['dt'].max().date() if not df_ib_dt['dt'].isna().all() else yesterday_dt

                if 'chart_date_mode' not in st.session_state:
                    st.session_state['chart_date_mode'] = "당월"
                    st.session_state['chart_start_date'] = default_start_dt
                    st.session_state['chart_end_date'] = yesterday_dt

                # 단일 기간 필터링 적용 (표와 차트에 동시 반영)
                curr_s = st.session_state['chart_start_date']
                curr_e = st.session_state['chart_end_date']

                filtered_df_ib_period = filtered_df_ib.copy()
                filtered_df_ib_period['dt_temp'] = pd.to_datetime(filtered_df_ib_period['영업마감일자'], errors='coerce').dt.date
                filtered_df_ib_period = filtered_df_ib_period[
                    (filtered_df_ib_period['dt_temp'] >= curr_s) & (filtered_df_ib_period['dt_temp'] <= curr_e)
                ]

                pivot_raw_ib1 = pd.pivot_table(filtered_df_ib_period, index=group_cols_ib, columns='영업마감일자', values=target_val, aggfunc='sum', fill_value=0)
                total_col_name = f"총 {target_val}"
                pivot_raw_ib1[total_col_name] = pivot_raw_ib1.sum(axis=1)
                
                pivot_ib1 = smart_fold_pivot_columns(pivot_raw_ib1, expanded_months_ib1)

                subtotal_dfs_ib = []
                c_375_ib = [c for c in pivot_ib1.index.get_level_values('센터').unique() if '1층' in str(c) or '375 1' in str(c)]
                if c_375_ib:
                    df_375_ib = pivot_ib1.loc[pivot_ib1.index.get_level_values('센터').isin(c_375_ib)]
                    subtotal_dfs_ib.append(df_375_ib)
                    sum_375_ib = df_375_ib.sum(axis=0)
                    sub_idx_375_ib = ("375 소계", "소계") if "보이기" in show_client_ib else "375 소계"
                    subtotal_dfs_ib.append(pd.DataFrame([sum_375_ib.values], columns=pivot_ib1.columns, index=pd.MultiIndex.from_tuples([sub_idx_375_ib], names=group_cols_ib) if "보이기" in show_client_ib else pd.Index([sub_idx_375_ib], name="센터")))

                c_xfc_ib = [c for c in pivot_ib1.index.get_level_values('센터').unique() if 'XFC' in str(c).upper()]
                if c_xfc_ib:
                    df_xfc_ib = pivot_ib1.loc[pivot_ib1.index.get_level_values('센터').isin(c_xfc_ib)]
                    subtotal_dfs_ib.append(df_xfc_ib)
                    sum_xfc_ib = df_xfc_ib.sum(axis=0)
                    sub_idx_xfc_ib = ("XFC 소계", "소계") if "보이기" in show_client_ib else "XFC 소계"
                    subtotal_dfs_ib.append(pd.DataFrame([sum_xfc_ib.values], columns=pivot_ib1.columns, index=pd.MultiIndex.from_tuples([sub_idx_xfc_ib], names=group_cols_ib) if "보이기" in show_client_ib else pd.Index([sub_idx_xfc_ib], name="센터")))

                c_other_ib = [c for c in pivot_ib1.index.get_level_values('센터').unique() if c not in c_375_ib and c not in c_xfc_ib]
                if c_other_ib:
                    df_other_ib = pivot_ib1.loc[pivot_ib1.index.get_level_values('센터').isin(c_other_ib)]
                    subtotal_dfs_ib.append(df_other_ib)

                body_df_ib = pd.concat(subtotal_dfs_ib) if subtotal_dfs_ib else pivot_ib1
                total_series_ib = pivot_ib1.sum(axis=0)
                total_label_ib = "★ 전체 합계"
                total_idx_ib = pd.MultiIndex.from_tuples([(total_label_ib, "전체")], names=group_cols_ib) if "보이기" in show_client_ib else pd.Index([total_label_ib], name="센터")
                total_df_ib = pd.DataFrame([total_series_ib.values], columns=pivot_ib1.columns, index=total_idx_ib)

                final_inbound = pd.concat([total_df_ib, body_df_ib])
                render_sticky_pivot(final_inbound, group_cols_ib, key_suffix="inbound_tab1")

                # ★ [표와 차트 사이: 빠른 기간 지정 및 단일 선택 컨트롤러 (동적 위젯 바인딩 고유키 적용)] ★
                st.markdown("<div style='margin-top: 30px; margin-bottom: 10px;'></div>", unsafe_allow_html=True)
                
                ch_hdr_col1, ch_hdr_col2 = st.columns([7, 3])
                with ch_hdr_col1:
                    st.markdown("<p style='font-size:14px; font-weight:bold; margin-bottom:5px;'>📅 조회 기간 지정 및 빠른 선택 (단일 선택):</p>", unsafe_allow_html=True)
                with ch_hdr_col2:
                    # ★ [그래프 확대 팝업 모달 버튼을 그래프 구역 상단 오른쪽에 단독 배치] ★
                    if st.button("🔍 그래프 전체화면 크게 보기 (팝업 모달)", key="btn_chart_modal_popup", type="primary", use_container_width=True):
                        show_popup_chart_modal(filtered_df_ib, curr_s, curr_e)

                cp1, cp2, cp3, cp4, cp5, cp6, cp7 = st.columns([1.8, 1.8, 1.0, 1.0, 1.0, 1.0, 1.0])

                with cp1:
                    ch_input_s = st.date_input("시작일자:", value=curr_s, key=f"chart_s_{curr_s}_{curr_e}", label_visibility="collapsed")
                with cp2:
                    ch_input_e = st.date_input("종료일자:", value=curr_e, key=f"chart_e_{curr_s}_{curr_e}", label_visibility="collapsed")

                if ch_input_s != curr_s or ch_input_e != curr_e:
                    st.session_state['chart_date_mode'] = "직접지정"
                    st.session_state['chart_start_date'] = ch_input_s
                    st.session_state['chart_end_date'] = ch_input_e
                    st.rerun()

                active_mode = st.session_state['chart_date_mode']

                def get_mode_btn_type(btn_mode):
                    return "primary" if active_mode == btn_mode else "secondary"

                c_need_update = False
                c_new_s, c_new_e, c_new_mode = curr_s, curr_e, active_mode

                with cp3:
                    if st.button("오늘", key="btn_chart_today", type=get_mode_btn_type("오늘"), use_container_width=True):
                        c_new_s, c_new_e, c_new_mode = yesterday_dt, yesterday_dt, "오늘"
                        c_need_update = True
                with cp4:
                    if st.button("일주일", key="btn_chart_week", type=get_mode_btn_type("일주일"), use_container_width=True):
                        c_new_s = max(ib_min_date, yesterday_dt - timedelta(days=6))
                        c_new_e, c_new_mode = yesterday_dt, "일주일"
                        c_need_update = True
                with cp5:
                    if st.button("1개월", key="btn_chart_1m", type=get_mode_btn_type("1개월"), use_container_width=True):
                        c_new_s = max(ib_min_date, yesterday_dt - timedelta(days=30))
                        c_new_e, c_new_mode = yesterday_dt, "1개월"
                        c_need_update = True
                with cp6:
                    if st.button("3개월", key="btn_chart_3m", type=get_mode_btn_type("3개월"), use_container_width=True):
                        c_new_s = max(ib_min_date, yesterday_dt - timedelta(days=90))
                        c_new_e, c_new_mode = yesterday_dt, "3개월"
                        c_need_update = True
                with cp7:
                    if st.button("전체 기간", key="btn_chart_all", type=get_mode_btn_type("전체 기간"), use_container_width=True):
                        c_new_s, c_new_e, c_new_mode = ib_min_date, ib_max_date, "전체 기간"
                        c_need_update = True

                if c_need_update:
                    st.session_state['chart_date_mode'] = c_new_mode
                    st.session_state['chart_start_date'] = c_new_s
                    st.session_state['chart_end_date'] = c_new_e
                    st.rerun()

                # ★ [Plotly.js 기반 범례 온/오프 인터랙티브 콤보 차트 표출 - 자동 리사이즈 적용] ★
                render_inbound_interactive_plotly_chart(filtered_df_ib, chart_start_date=st.session_state['chart_start_date'], chart_end_date=st.session_state['chart_end_date'])

        else:
            st.info("입고 데이터가 존재하지 않습니다. 구글 드라이브에 입고요청서 엑셀 파일을 올린 후 [🔄 드라이브 & 구글시트 동기화]를 눌러주세요.")

    with in_tab2:
        st.header("📋 상태별(입고완료 / 승인대기) 현황")
        if not df_inbound.empty and '상태' in df_inbound.columns:
            expanded_months_ib2 = render_month_button_bar(
                df_inbound, 
                session_key_selected="ib2_exp_months", 
                title_label="🗓️ 상세 일자 펼침 월 선택 (미선택 시 월합계만 접힘):", 
                allow_nujak=False, 
                multi_select=True
            )

            pivot_raw_ib2 = pd.pivot_table(df_inbound, index=['센터', '고객사', '상태'], columns='영업마감일자', values='입고완료수량', aggfunc='sum', fill_value=0)
            pivot_raw_ib2['총 입고완료수량'] = pivot_raw_ib2.sum(axis=1)
            
            pivot_ib2 = smart_fold_pivot_columns(pivot_raw_ib2, expanded_months_ib2)

            render_sticky_pivot(pivot_ib2, ['센터', '고객사', '상태'], key_suffix="inbound_tab2")
        else:
            st.info("입고 상태 데이터가 없습니다.")
