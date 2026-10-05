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

st.set_page_config(page_title="B2C 출고현황 동적 대시보드", layout="wide")

# CSS: 2D 틀고정 (상단 헤더/합계 고정 + 좌측 센터/고객사 고정)
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

    /* 1. 컬럼 헤더 상단 고정 */
    .sticky-table thead tr th {
        position: sticky;
        top: 0;
        z-index: 20;
        background-color: #1f2937 !important;
        color: #9ca3af;
        font-weight: bold;
    }

    /* 2. ★ 일별 합계 행 상단 고정 */
    .sticky-table tr.total-row td {
        position: sticky;
        top: 35px;
        z-index: 15;
        background-color: #1e293b !important;
        color: #facc15 !important;
        font-weight: bold;
        border-bottom: 2px solid #eab308 !important;
    }

    /* 3. 좌측 1번째 열 (센터) 고정 */
    .sticky-table th.freeze-col-1, 
    .sticky-table td.freeze-col-1 {
        position: sticky;
        left: 0;
        z-index: 10;
        background-color: #111827 !important;
        border-right: 2px solid #374151 !important;
        text-align: left;
    }

    /* 4. 좌측 2번째 열 (고객사) 고정 */
    .sticky-table th.freeze-col-2, 
    .sticky-table td.freeze-col-2 {
        position: sticky;
        left: 140px; /* 1번째 열 너비 고려 */
        z-index: 10;
        background-color: #111827 !important;
        border-right: 2px solid #374151 !important;
        text-align: left;
    }

    /* 5. 교차 모서리 (좌측 고정 열 + 상단 고정 헤더/합계) z-index 최우선 처리 */
    .sticky-table thead tr th.freeze-col-1,
    .sticky-table thead tr th.freeze-col-2 {
        z-index: 30 !important;
        background-color: #1f2937 !important;
    }
    .sticky-table tr.total-row td.freeze-col-1,
    .sticky-table tr.total-row td.freeze-col-2 {
        z-index: 25 !important;
        background-color: #1e293b !important;
    }

    /* 소계 행 스타일 */
    .sticky-table tr.subtotal-row td {
        background-color: #0f172a !important;
        color: #38bdf8 !important;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

DB_PATH = "wms_dashboard.db"

def run_sync():
    if "gcp_service_account" in st.secrets:
        try:
            creds_dict = dict(st.secrets["gcp_service_account"])
            service = etl_pipeline.get_drive_service(creds_dict)
            
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(current, total, filename, eta):
                if total > 0:
                    pct = int((current / total) * 100)
                    progress_bar.progress(pct)
                    mins, secs = divmod(eta, 60)
                    eta_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
                    status_text.markdown(f"⏳ **데이터 동기화 중 ({pct}%)** - `{current}/{total}`개 완료\n\n📄 **처리 중**: `{filename}` | ⏱️ **남은 시간**: 약 **{eta_str}**")
                else:
                    status_text.info("처리할 새로운 엑셀 파일이 없습니다.")

            etl_pipeline.process_and_update(service, progress_callback=update_progress)
            
            progress_bar.empty()
            status_text.empty()
            st.cache_data.clear()
            return True
        except Exception as e:
            st.sidebar.error(f"동기화 에러: {e}")
            return False
    return False

# 최초 접속 시 드라이브에서 DB 가져오기
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
def load_data():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("SELECT * FROM daily_summary", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df

st.title("🚚 B2C 출고현황 동적 대시보드")

# --- 사이드바 동기화 & 대시보드 상태 ---
if st.sidebar.button("🔄 드라이브 동기화 / 새로고침"):
    run_sync()
    st.rerun()

df_raw = load_data()

if df_raw.empty:
    st.sidebar.warning("⚠️ 집계된 데이터가 없습니다.")
    st.info("구글 드라이브 폴더에 새 엑셀 파일을 올린 후 [🔄 드라이브 동기화 / 새로고침] 버튼을 눌러주세요.")
else:
    st.sidebar.success(f"데이터 로드 성공! (총 {len(df_raw):,}개 집계 레코드)")

# --- 사이드바: 엑셀 다운로드 전용 섹션 ---
st.sidebar.markdown("---")
st.sidebar.subheader("📥 엑셀 데이터 다운로드")

if not df_raw.empty:
    all_cols = ['영업마감일자', '센터', '고객사', '바코드', 'SKU명', '출고박스종류', '배송속성', '판매처', '출고건수', '총출고수량']
    selected_cols = st.sidebar.multiselect(
        "다운로드할 항목 선택:", 
        all_cols, 
        default=['영업마감일자', '센터', '고객사', '바코드', 'SKU명', '출고건수', '총출고수량']
    )
    
    if selected_cols:
        group_keys = [c for c in selected_cols if c not in ['출고건수', '총출고수량']]
        val_keys = [c for c in ['출고건수', '총출고수량'] if c in selected_cols]
        
        if group_keys and val_keys:
            export_df = df_raw.groupby(group_keys)[val_keys].sum().reset_index()
        else:
            export_df = df_raw[selected_cols]
            
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            export_df.to_excel(writer, index=False, sheet_name='출고상세현황')
            
        st.sidebar.download_button(
            label="💾 선택한 항목으로 엑셀 다운로드",
            data=output.getvalue(),
            file_name=f"B2C_출고현황_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

# 2D 틀고정 HTML 스티키 테이블 렌더링 함수
def render_sticky_pivot(df, index_names):
    html = ['<div class="sticky-table-container"><table class="sticky-table"><thead><tr>']
    
    num_freeze_cols = len(index_names)
    
    for idx_i, idx_name in enumerate(index_names, 1):
        freeze_cls = f' class="freeze-col-{idx_i}"' if idx_i <= 2 else ''
        html.append(f'<th{freeze_cls}>{idx_name}</th>')
    
    cols = [c for c in df.columns]
    for c in cols:
        html.append(f'<th>{c}</th>')
    html.append('</tr></thead><tbody>')
    
    for idx_val, row in df.iterrows():
        is_total = "★ 일별 합계" in str(idx_val)
        is_subtotal = "소계" in str(idx_val)
        
        row_class = ' class="total-row"' if is_total else (' class="subtotal-row"' if is_subtotal else '')
        html.append(f'<tr{row_class}>')
        
        if isinstance(idx_val, tuple):
            for idx_i, v in enumerate(idx_val, 1):
                freeze_cls = f' class="freeze-col-{idx_i}"' if idx_i <= 2 else ''
                html.append(f'<td{freeze_cls}>{v}</td>')
        else:
            freeze_cls = ' class="freeze-col-1"'
            html.append(f'<td{freeze_cls}>{idx_val}</td>')
            
        for val in row:
            val_str = f"{int(val):,}" if pd.notnull(val) and isinstance(val, (int, float)) else str(val)
            html.append(f'<td>{val_str}</td>')
        html.append('</tr>')
        
    html.append('</tbody></table></div>')
    st.markdown("".join(html), unsafe_allow_html=True)

# --- 메인 탭 화면 ---
tab1, tab2, tab3, tab4 = st.tabs([
    "📊 센터/고객사별 일자 출고현황", 
    "🚚 배송속성 / 판매처별 현황", 
    "📦 출고박스별 현황",
    "🔍 SKU별 출고량"
])

# Tab 1: 센터/고객사별 일자 출고현황 (2D 틀고정)
with tab1:
    st.header("센터 & 고객사별 일자 출고현황 (06시 영업마감 기준)")
    if not df_raw.empty:
        col1, col2, col3 = st.columns([2, 2, 2])
        with col1:
            centers = st.multiselect("1. 센터 선택 (미선택 시 전체)", sorted(list(df_raw['센터'].dropna().unique())), key="tab1_centers")
        with col2:
            show_client = st.radio("2. 고객사 구분 표시", ["숨김 (센터별 요약)", "보이기 (고객사 상세)"])
        with col3:
            if "보이기" in show_client:
                available_clients_df = df_raw[df_raw['센터'].isin(centers)] if centers else df_raw
                available_clients = sorted(list(available_clients_df['고객사'].dropna().unique()))
                clients = st.multiselect("3. 고객사 선택 (미선택 시 전체)", available_clients, key="tab1_clients")
            else:
                clients = []
                st.selectbox("3. 고객사 선택", ["고객사 숨김 상태"], disabled=True)

        filtered_df = df_raw.copy()
        if centers: filtered_df = filtered_df[filtered_df['센터'].isin(centers)]
        if "보이기" in show_client and clients: filtered_df = filtered_df[filtered_df['고객사'].isin(clients)]

        group_cols = ['센터']
        if "보이기" in show_client: group_cols.append('고객사')

        if not filtered_df.empty:
            pivot_df = pd.pivot_table(filtered_df, index=group_cols, columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
            pivot_df['총 출고건수'] = pivot_df.sum(axis=1)

            # --- 소계(부분합) 행 생성 ---
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

            # 맨 위 일별 합계 행 생성
            total_series = pivot_df.sum(axis=0)
            total_idx = pd.MultiIndex.from_tuples([("★ 일별 합계", "전체")], names=group_cols) if "보이기" in show_client else pd.Index(["★ 일별 합계"], name="센터")
            total_df = pd.DataFrame([total_series.values], columns=pivot_df.columns, index=total_idx)

            final_df = pd.concat([total_df, body_df])
            
            # 2D 틀고정 테이블 출력
            render_sticky_pivot(final_df, group_cols)

# Tab 2: 배송속성 / 판매처별 현황
with tab2:
    st.header("배송 속성 및 판매처별 출고현황")
    if not df_raw.empty:
        analysis_type = st.radio("분석 기준 선택", ["배송 속성별", "판매처별"], horizontal=True)
        target_col = '배송속성' if analysis_type == "배송 속성별" else '판매처'
        
        pivot_df2 = pd.pivot_table(df_raw, index=[target_col], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
        pivot_df2['총 출고건수'] = pivot_df2.sum(axis=1)
        total_series2 = pivot_df2.sum(axis=0)
        total_df2 = pd.DataFrame([total_series2.values], columns=pivot_df2.columns, index=pd.Index(["★ 일별 합계"], name=target_col))
        
        final_df2 = pd.concat([total_df2, pivot_df2])
        render_sticky_pivot(final_df2, [target_col])

# Tab 3: 출고박스별 현황
with tab3:
    st.header("출고박스 규격별 사용 현황")
    if not df_raw.empty:
        pivot_df3 = pd.pivot_table(df_raw, index=['출고박스종류'], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
        pivot_df3['총 출고건수'] = pivot_df3.sum(axis=1)
        total_series3 = pivot_df3.sum(axis=0)
        total_df3 = pd.DataFrame([total_series3.values], columns=pivot_df3.columns, index=pd.Index(["★ 일별 합계"], name="출고박스 규격"))
        
        final_df3 = pd.concat([total_df3, pivot_df3])
        render_sticky_pivot(final_df3, ["출고박스 규격"])

# Tab 4: SKU별 출고량
with tab4:
    st.header("🔍 SKU별 출고량 (기간 선택 집계)")
    if not df_raw.empty:
        df_raw['영업마감일자_dt'] = pd.to_datetime(df_raw['영업마감일자'], errors='coerce')
        min_date = df_raw['영업마감일자_dt'].min().date() if not df_raw['영업마감일자_dt'].isna().all() else datetime.now().date()
        max_date = df_raw['영업마감일자_dt'].max().date() if not df_raw['영업마감일자_dt'].isna().all() else datetime.now().date()

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

        col1, col2 = st.columns(2)
        with col1:
            selected_centers = st.multiselect("센터 선택 (다중 선택 가능)", sorted(list(df_raw['센터'].dropna().unique())), key="tab4_centers")
        
        filtered_by_center = df_raw[df_raw['센터'].isin(selected_centers)] if selected_centers else df_raw
        available_clients = sorted(list(filtered_by_center['고객사'].dropna().unique()))
        
        with col2:
            selected_clients = st.multiselect("고객사 선택 (선택한 센터의 고객사만 표시)", available_clients, key="tab4_clients")
            
        sku_df = df_raw.copy()
        
        if start_date and end_date:
            sku_df = sku_df[(sku_df['영업마감일자_dt'].dt.date >= start_date) & (sku_df['영업마감일자_dt'].dt.date <= end_date)]

        if selected_centers:
            sku_df = sku_df[sku_df['센터'].isin(selected_centers)]
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
        else:
            st.info("선택한 조건 및 기간에 해당하는 데이터가 없습니다.")
