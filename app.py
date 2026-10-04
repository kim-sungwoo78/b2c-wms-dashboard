import os
import io
import json
import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime
import etl_pipeline
import importlib

importlib.reload(etl_pipeline)

st.set_page_config(page_title="B2C 출고현황 동적 대시보드", layout="wide")

st.markdown("""
<style>
    [data-testid="stDataFrame"] table tbody tr:first-child {
        position: sticky !important; top: 0 !important; z-index: 10 !important;
        background-color: #1e222a !important; font-weight: bold !important;
        border-bottom: 2px solid #4a5568 !important;
    }
</style>
""", unsafe_allow_html=True)

DB_PATH = "wms_dashboard.db"

def run_sync():
    if "gcp_service_account" in st.secrets:
        try:
            creds_dict = dict(st.secrets["gcp_service_account"])
            service = etl_pipeline.get_drive_service(creds_dict)
            etl_pipeline.process_and_update(service)
            st.cache_data.clear()
            return True
        except Exception as e:
            st.sidebar.error(f"동기화 에러: {e}")
            return False
    return False

# 최초 접속 시 파일 자동 동기화
if "initial_synced" not in st.session_state:
    with st.spinner("구글 드라이브 최신 데이터 동기화 중..."):
        run_sync()
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

if st.sidebar.button("🔄 드라이브 동기화 / 새로고침"):
    with st.spinner("드라이브 데이터 동기화 중..."):
        run_sync()
    st.rerun()

df_raw = load_data()

if df_raw.empty:
    st.sidebar.warning("⚠️ 집계된 데이터가 없습니다.")
    st.info("구글 드라이브 폴더에 새 엑셀 파일을 올린 후 [🔄 드라이브 동기화 / 새로고침] 버튼을 눌러주세요.")
else:
    st.sidebar.success(f"데이터 로드 성공! (총 {len(df_raw):,}개 집계 레코드)")

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 센터/고객사별 일자 출고현황", 
    "🚚 배송속성 / 판매처별 현황", 
    "📦 출고박스별 현황",
    "🔍 센터/고객사/SKU 상세 & 엑셀 다운로드"
])

with tab1:
    st.header("센터 & 고객사별 일자 출고현황 (06시 영업마감 기준)")
    if not df_raw.empty:
        col1, col2, col3 = st.columns([2, 2, 2])
        with col1:
            centers = st.multiselect("1. 센터 선택 (미선택 시 전체)", sorted(list(df_raw['센터'].dropna().unique())))
        with col2:
            show_client = st.radio("2. 고객사 구분 표시", ["숨김 (센터별 요약)", "보이기 (고객사 상세)"])
        with col3:
            if "보이기" in show_client:
                clients = st.multiselect("3. 고객사 선택 (미선택 시 전체)", sorted(list(df_raw['고객사'].dropna().unique())))
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
            
            total_series = pivot_df.sum(axis=0)
            total_idx = pd.MultiIndex.from_tuples([("★ 일별 합계", "전체")], names=group_cols) if len(group_cols) > 1 else pd.Index(["★ 일별 합계"], name="센터")
            total_df = pd.DataFrame([total_series.values], columns=pivot_df.columns, index=total_idx)
            
            final_df = pd.concat([total_df, pivot_df])
            final_df.set_index('총 출고건수', append=True, inplace=True)
            st.dataframe(final_df, use_container_width=True)

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
        final_df2.set_index('총 출고건수', append=True, inplace=True)
        st.dataframe(final_df2, use_container_width=True)

with tab3:
    st.header("출고박스 규격별 사용 현황")
    if not df_raw.empty:
        pivot_df3 = pd.pivot_table(df_raw, index=['출고박스종류'], columns='영업마감일자', values='출고건수', aggfunc='sum', fill_value=0)
        pivot_df3['총 출고건수'] = pivot_df3.sum(axis=1)
        total_series3 = pivot_df3.sum(axis=0)
        total_df3 = pd.DataFrame([total_series3.values], columns=pivot_df3.columns, index=pd.Index(["★ 일별 합계"], name="출고박스 규격"))
        final_df3 = pd.concat([total_df3, pivot_df3])
        final_df3.set_index('총 출고건수', append=True, inplace=True)
        st.dataframe(final_df3, use_container_width=True)

with tab4:
    st.header("🔍 센터/고객사/SKU 상세 조회 및 선택 엑셀 다운로드")
    if not df_raw.empty:
        all_cols = ['영업마감일자', '센터', '고객사', '바코드', 'SKU명', '출고박스종류', '배송속성', '판매처', '출고건수', '총출고수량']
        selected_cols = st.multiselect("다운로드할 항목을 선택하세요:", all_cols, default=['영업마감일자', '센터', '고객사', '바코드', 'SKU명', '출고건수', '총출고수량'])
        
        if selected_cols:
            group_keys = [c for c in selected_cols if c not in ['출고건수', '총출고수량']]
            val_keys = [c for c in ['출고건수', '총출고수량'] if c in selected_cols]
            export_df = df_raw.groupby(group_keys)[val_keys].sum().reset_index() if group_keys else df_raw[val_keys]
            
            st.dataframe(export_df.head(1000), use_container_width=True)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                export_df.to_excel(writer, index=False, sheet_name='출고상세현황')
                
            st.download_button(
                label="💾 선택한 항목으로 엑셀 파일 다운로드",
                data=output.getvalue(),
                file_name=f"B2C_출고현황_{datetime.now().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
