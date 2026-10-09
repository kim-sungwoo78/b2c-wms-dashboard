import os
import io
import sqlite3
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime

# etl_pipeline 모듈 연동
try:
    from etl_pipeline import (
        get_drive_service,
        get_sheets_service,
        process_and_update,
        sync_db_from_drive,
        DB_B2C_PATH,
        DB_INBOUND_PATH
    )
except ImportError:
    pass

st.set_page_config(
    page_title="센터 통합 물류 운영 대시보드",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS 커스텀 디자인 스타일
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #FFFFFF;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #AAAAAA;
        margin-bottom: 1.5rem;
    }
    .metric-container {
        background-color: #1E1E1E;
        border: 1px solid #333333;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
    }
    .metric-label {
        font-size: 0.95rem;
        color: #888888;
        font-weight: 600;
        margin-bottom: 8px;
    }
    .metric-value {
        font-size: 2.2rem;
        font-weight: bold;
        color: #4CAF50;
    }
    .metric-sub {
        font-size: 0.8rem;
        color: #AAAAAA;
        margin-top: 5px;
    }
</style>
""", unsafe_allow_html=True)

# 1. 인증 및 서비스 연결
@st.cache_resource
def init_services():
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        drive_service = get_drive_service(creds_dict)
        sheets_service = get_sheets_service(creds_dict)
        if drive_service:
            sync_db_from_drive(drive_service)
        return drive_service, sheets_service
    except Exception:
        return None, None

drive_service, sheets_service = init_services()

# 2. 사이드바
with st.sidebar:
    st.image("https://img.icons8.com/color/96/000000/warehouse.png", width=70)
    st.title("⚙️ 시스템 설정")
    st.markdown("---")
    
    st.subheader("🔄 데이터 동기화")
    st.caption("구글 드라이브 B2C 엑셀 및 입고 구글시트를 수집하여 DB를 재구축합니다.")
    
    if st.button("🔄 드라이브 & 구글시트 동기화", use_container_width=True, type="primary"):
        if not drive_service:
            st.error("❌ Google 인증 정보가 설정되지 않았습니다.")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(current, total, filename, msg):
                percent = int((current / total) * 100) if total > 0 else 100
                progress_bar.progress(percent)
                status_text.text(f"⏳ 동기화 진행 중 ({current}/{total})\n📄 {filename}")

            with st.spinner("구글 드라이브 동기화 진행 중..."):
                matched_cnt, msg = process_and_update(drive_service, sheets_service, update_progress)
                progress_bar.empty()
                status_text.empty()
                st.cache_data.clear()
                st.success(f"{msg}")
                st.rerun()

    st.markdown("---")
    st.info("""
    - **B2C 출고**: 구글 드라이브 로우파일 자동 집계
    - **입고 현황**: 구글 스마트 시트 실시간 연동
    - **B2B 연동**: 시스템 연결 구성 중
    """)

# 헤더
st.markdown('<div class="main-header">🏢 센터 통합 물류 운영 대시보드</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">실시간 B2C 출고 데이터 및 입고 운영 실적 모니터링</div>', unsafe_allow_html=True)

# 3. 글로벌 필터
st.subheader("📊 조회 필터 설정")
col_f1, col_f2 = st.columns([1, 2])

with col_f1:
    centers = ["전체"]
    if os.path.exists("wms_b2c.db"):
        try:
            conn_c = sqlite3.connect("wms_b2c.db")
            df_c = pd.read_sql("SELECT DISTINCT 센터 FROM daily_summary WHERE 센터 IS NOT NULL", conn_c)
            conn_c.close()
            if not df_c.empty:
                for val in sorted(df_c['센터'].dropna().unique()):
                    if val not in centers:
                        centers.append(val)
        except Exception:
            pass
    selected_center = st.selectbox("🏬 센터 선택 (미선택 시 전체):", centers, index=0)

with col_f2:
    current_month_num = datetime.now().month
    selected_month = st.radio(
        "📅 기준 월 선택:",
        options=list(range(1, 13)),
        index=current_month_num - 1,
        format_func=lambda x: f"{x}월",
        horizontal=True
    )

st.markdown("---")

# 4. DB 로딩
def load_b2c_data(selected_m, center_filter):
    if not os.path.exists("wms_b2c.db"):
        return pd.DataFrame()

    try:
        conn = sqlite3.connect("wms_b2c.db")
        df_summary = pd.read_sql("SELECT * FROM daily_summary", conn)
        conn.close()

        if df_summary.empty:
            return pd.DataFrame()

        df_summary['영업마감일자'] = pd.to_datetime(df_summary['영업마감일자'], errors='coerce')
        df_filtered = df_summary[df_summary['영업마감일자'].dt.month == selected_m].copy()

        if center_filter != "전체":
            df_filtered = df_filtered[df_filtered['센터'] == center_filter]

        return df_filtered
    except Exception:
        return pd.DataFrame()

def load_inbound_data(selected_m):
    if not os.path.exists("wms_inbound.db"):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect("wms_inbound.db")
        df = pd.read_sql("SELECT * FROM inbound_summary", conn)
        conn.close()
        if df.empty:
            return df
        if '입고일자' in df.columns:
            df['입고일자'] = pd.to_datetime(df['입고일자'], errors='coerce')
            df = df[df['입고일자'].dt.month == selected_m]
        return df
    except Exception:
        return pd.DataFrame()

df_b2c = load_b2c_data(selected_month, selected_center)
df_ib = load_inbound_data(selected_month)

total_b2c_cnt = int(df_b2c['출고건수'].sum()) if not df_b2c.empty and '출고건수' in df_b2c.columns else 0
total_b2c_qty = int(df_b2c['총출고수량'].sum()) if not df_b2c.empty and '총출고수량' in df_b2c.columns else 0
total_ib_cnt = len(df_ib) if not df_ib.empty else 0
total_plt_cnt = float(df_ib['PLT수'].sum()) if not df_ib.empty and 'PLT수' in df_ib.columns else 0.0
total_box_cnt = float(df_ib['BOX수'].sum()) if not df_ib.empty and 'BOX수' in df_ib.columns else 0.0

kpi1, kpi2, kpi3 = st.columns(3)

with kpi1:
    st.markdown(f"""
    <div class="metric-container">
        <div class="metric-label">🚚 B2C 출고건수 ({selected_month}월)</div>
        <div class="metric-value">{total_b2c_cnt:,} <span style="font-size:1.2rem; color:#fff;">건</span></div>
        <div class="metric-sub">총 출고 수량: {total_b2c_qty:,} 개</div>
    </div>
    """, unsafe_allow_html=True)

with kpi2:
    st.markdown(f"""
    <div class="metric-container">
        <div class="metric-label">📦 입고 완료 건수 ({selected_month}월)</div>
        <div class="metric-value" style="color:#2196F3;">{total_ib_cnt:,} <span style="font-size:1.2rem; color:#fff;">건</span></div>
        <div class="metric-sub">PLT: {total_plt_cnt:,.1f} / BOX: {total_box_cnt:,.0f}</div>
    </div>
    """, unsafe_allow_html=True)

with kpi3:
    st.markdown(f"""
    <div class="metric-container">
        <div class="metric-label">🏭 B2B 출고건수</div>
        <div class="metric-value" style="color:#FF9800;">0 <span style="font-size:1.2rem; color:#fff;">건</span></div>
        <div class="metric-sub">연동 모듈 구성 대기 중</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# 5. 탭 구성
tab1, tab2, tab3 = st.tabs(["🏛️ 메인: 센터 종합 현황", "🚚 B2C 출고 현황", "📦 입고 현황"])

with tab1:
    st.markdown(f"### 📊 {selected_month}월 센터별 운영 현황 요약")
    
    if df_b2c.empty and df_ib.empty:
        st.info(f"ℹ️ {selected_month}월 집계 데이터가 없습니다. 사이드바의 [동기화] 버튼을 클릭해 주세요.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### 🚚 고객사별 B2C 출고 TOP 10")
            if not df_b2c.empty and '고객사' in df_b2c.columns:
                cust_df = df_b2c.groupby('고객사', as_index=False)['출고건수'].sum().sort_values('출고건수', ascending=False).head(10)
                st.dataframe(cust_df, use_container_width=True, hide_index=True)
                st.bar_chart(cust_df.set_index('고객사')['출고건수'])
            else:
                st.caption("B2C 집계 데이터가 없습니다.")

        with c2:
            st.markdown("#### 📦 일자별 B2C 출고 추이")
            if not df_b2c.empty and '영업마감일자' in df_b2c.columns:
                df_b2c_copy = df_b2c.copy()
                df_b2c_copy['일자'] = df_b2c_copy['영업마감일자'].dt.strftime('%m-%d')
                daily_df = df_b2c_copy.groupby('일자', as_index=False)['출고건수'].sum()
                st.dataframe(daily_df, use_container_width=True, hide_index=True)
                st.line_chart(daily_df.set_index('일자')['출고건수'])
            else:
                st.caption("일자별 집계 데이터가 없습니다.")

with tab2:
    st.markdown(f"### 🚚 B2C 출고 상세 내역 ({selected_month}월)")
    
    if not df_b2c.empty:
        col_s1, col_s2, col_s3 = st.columns([2, 2, 1])
        with col_s1:
            search_customer = st.text_input("🔍 고객사 검색:", "")
        with col_s2:
            search_sku = st.text_input("🔍 SKU/상품명 검색:", "")
            
        filtered_df = df_b2c.copy()
        if search_customer:
            filtered_df = filtered_df[filtered_df['고객사'].astype(str).str.contains(search_customer, case=False, na=False)]
        if search_sku and 'SKU명' in filtered_df.columns:
            filtered_df = filtered_df[filtered_df['SKU명'].astype(str).str.contains(search_sku, case=False, na=False)]

        with col_s3:
            st.markdown("<br>", unsafe_allow_html=True)
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                filtered_df.to_excel(writer, sheet_name=f'B2C_Export_{selected_month}월', index=False)
            excel_bytes = output.getvalue()

            st.download_button(
                label="📥 엑셀 다운로드",
                data=excel_bytes,
                file_name=f"B2C_Summary_{selected_month}월_{selected_center}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

        display_df = filtered_df.copy()
        if '영업마감일자' in display_df.columns:
            display_df['영업마감일자'] = display_df['영업마감일자'].dt.strftime('%Y-%m-%d')

        st.dataframe(display_df, use_container_width=True, hide_index=True)
    else:
        st.info("선택된 월의 B2C 출고 데이터가 없습니다.")

with tab3:
    st.markdown(f"### 📦 입고 상세 내역 ({selected_month}월)")
    
    if not df_ib.empty:
        col_ib1, col_ib2 = st.columns([3, 1])
        with col_ib1:
            search_ib_no = st.text_input("🔍 입고번호 검색:", "")
            
        ib_filtered = df_ib.copy()
        if search_ib_no and '입고번호' in ib_filtered.columns:
            ib_filtered = ib_filtered[ib_filtered['입고번호'].astype(str).str.contains(search_ib_no, case=False, na=False)]

        with col_ib2:
            st.markdown("<br>", unsafe_allow_html=True)
            output_ib = io.BytesIO()
            with pd.ExcelWriter(output_ib, engine='openpyxl') as writer:
                ib_filtered.to_excel(writer, sheet_name=f'입고_Export_{selected_month}월', index=False)
            excel_bytes_ib = output_ib.getvalue()

            st.download_button(
                label="📥 입고 엑셀 다운로드",
                data=excel_bytes_ib,
                file_name=f"Inbound_Summary_{selected_month}월.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

        display_ib = ib_filtered.copy()
        if '입고일자' in display_ib.columns:
            display_ib['입고일자'] = display_ib['입고일자'].dt.strftime('%Y-%m-%d')

        st.dataframe(display_ib, use_container_width=True, hide_index=True)
    else:
        st.info("선택된 월의 입고 데이터가 없습니다.")
