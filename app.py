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

DB_PATH = "wms_dashboard.db"

st.title("🚚 B2C 출고현황 동적 대시보드 (진단 모드)")

# 비밀키(Secrets) 확인
if "gcp_service_account" not in st.secrets:
    st.error("❌ Streamlit Secrets에 [gcp_service_account]가 설정되지 않았습니다.")
    st.stop()

st.sidebar.success("✅ GCP Secrets 정상 로드됨")

# 동기화 실행 및 에러 출력
if st.sidebar.button("🔄 드라이브 동기화 실행"):
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        service = etl_pipeline.get_drive_service(creds_dict)
        
        # 1. 구글 드라이브 파일 검색 테스트
        query = f"'{etl_pipeline.MAIN_UPLOAD_FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])
        
        st.write(f"📂 **구글 드라이브 인식 파일 수**: {len(files)}개")
        for f in files:
            st.write(f"- {f['name']} (ID: {f['id']})")
            
        # 2. ETL 파이프라인 실행
        etl_pipeline.process_and_update(service)
        st.success("🎉 데이터 동기화 완료!")
        st.cache_data.clear()
        
    except Exception as e:
        st.error(f"❌ 동기화 중 에러 발생: {e}")
        st.exception(e)

# DB 조회
conn = sqlite3.connect(DB_PATH)
try:
    df_raw = pd.read_sql("SELECT * FROM daily_summary", conn)
    st.write("📊 **현재 DB 집계 데이터 (daily_summary)**:")
    st.dataframe(df_raw)
except Exception as e:
    st.warning(f"DB 데이터 로드 실패: {e}")
finally:
    conn.close()
