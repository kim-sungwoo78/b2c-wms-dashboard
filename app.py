import os
import io
import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime

# etl_pipeline 임포트
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

st.set_page_config(page_title="센터 통합 물류 운영 대시보드", layout="wide")

st.markdown("""
<style>
    .metric-card {
        background-color: #1e1e1e;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 10px;
    }
    .metric-title { color: #888; font-size: 0.9rem; }
    .metric-value { font-size: 1.8rem; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

st.title("🏢 센터 통합 물류 운영 대시보드")

# 1. 인증 및 서버 시작 시 DB 구글 드라이브 동기화
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

# 2. 동기화 사이드바
with st.sidebar:
    st.header("🔄 데이터 동기화")
    if st.button("🔄 드라이브 & 구글시트 동기화", use_container_width=True):
        if not drive_service:
            st.error("❌ Google 인증 정보를 확인해주세요.")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(current, total, filename, msg):
                percent = int((current / total) * 100) if total > 0 else 100
                progress_bar.progress(percent)
                status_text.text(f"⏳ 동기화 진행 중 ({current}/{total})\n📄 {filename}")

            with st.spinner("구글 드라이브 동기화 중..."):
                matched_cnt, msg = process_and_update(drive_service, sheets_service, update_progress)
                progress_bar.empty()
                status_text.empty()
                st.cache_data.clear()
                st.success(f"{msg}")
                st.rerun()

# 3. 필터 설정
st.subheader("📊 센터 종합 운영 실적 요약")

col_f1, col_f2 = st.columns([1, 2])

with col_f1:
    centers = ["전체"]
    if os.path.exists("wms_b2c.db"):
        try:
            conn_c = sqlite3.connect("wms_b2c.db")
            df_c = pd.read_sql("SELECT DISTINCT 센터 FROM daily_summary WHERE 센터 IS NOT NULL", conn_c)
            conn_c.close()
            if not df_c.empty:
                for val in df_c['센터'].dropna().unique():
                    if val not in centers:
                        centers.append(val)
        except Exception:
            pass
    selected_center = st.selectbox("🏬 센터 선택:", centers, index=0)

with col_f2:
    current_month_num = datetime.now().month
    selected_month = st.radio(
        "📅 기준 월 선택:",
        options=list(range(1, 13)),
        index=current_month_num - 1,
        format_func=lambda x: f"{x}월",
        horizontal=True
    )

# 4. DB 데이터 읽기
def load_b2c_data(selected_m, center_filter):
    if not os.path.exists("wms_b2c.db"):
        return pd.DataFrame()

    try:
        conn = sqlite3.connect("wms_b2c.db")
        df_all = pd.read_sql("SELECT * FROM daily_summary", conn)
        conn.close()

        if df_all.empty:
            return df_all

        df_all['영업마감일자'] = pd.to_datetime(df_all['영업마감일자'], errors='coerce')
        df_filtered = df_all[df_all['영업마감일자'].dt.month == selected_m]

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
total_ib_cnt = len(df_ib) if not df_ib.empty else 0

mc1, mc2, mc3 = st.columns(3)
with mc1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">🚚 B2C 출고건수 ({selected_month}월)</div>
        <div class="metric-value">{total_b2c_cnt:,} 건</div>
    </div>
    """, unsafe_allow_html=True)

with mc2:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">📦 입고건수 ({selected_month}월)</div>
        <div class="metric-value">{total_ib_cnt:,} 건</div>
    </div>
    """, unsafe_allow_html=True)

with mc3:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">🏭 B2B 출고건수</div>
        <div class="metric-value">0 건 (연동 준비중)</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# 5. 탭 구성
tab1, tab2, tab3 = st.tabs(["🏛️ 메인: 센터 종합 현황", "🚚 B2C 출고 현황", "📦 입고 현황"])

with tab1:
    st.markdown(f"### 📊 {selected_month}월 물류 운영 종합 현황")
    if df_b2c.empty:
        st.info(f"ℹ️ {selected_month}월 집계 데이터가 없습니다. 동기화 버튼을 클릭해 주세요.")
    else:
        st.dataframe(df_b2c, use_container_width=True)

with tab2:
    st.markdown(f"### 🚚 B2C 출고 상세 현황 ({selected_month}월)")
    if not df_b2c.empty:
        col_t2_1, col_t2_2 = st.columns([3, 1])
        with col_t2_2:
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_b2c.to_excel(writer, sheet_name=f'B2C_Raw', index=False)
            excel_bytes = output.getvalue()

            st.download_button(
                label="📥 선택 기간 원본 데이터 엑셀 다운로드",
                data=excel_bytes,
                file_name=f"B2C_Data_{selected_month}월_{selected_center}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        st.dataframe(df_b2c, use_container_width=True)
    else:
        st.info("표시할 B2C 데이터가 없습니다.")

with tab3:
    st.markdown(f"### 📦 입고 상세 현황 ({selected_month}월)")
    if not df_ib.empty:
        st.dataframe(df_ib, use_container_width=True)
    else:
        st.info("표시할 입고 데이터가 없습니다.")
