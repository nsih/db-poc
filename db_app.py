import streamlit as st
import db_builder
from utils import load_engine

st.set_page_config(page_title="CSU DB Console", layout="wide")

try:
    engine = load_engine()
except db_builder.DbBuilderError as e:
    st.error(f"DB 연결 실패: {e}")
    st.stop()

# 사이드바 — 테이블 목록
with st.sidebar:
    st.markdown("---")
    st.caption("📋 테이블 목록")
    try:
        tables = db_builder.list_tables(engine)
        if tables:
            for t in tables:
                st.caption(f"• {t}")
        else:
            st.caption("(테이블 없음)")
    except Exception:
        st.caption("조회 실패")

nl_page  = st.Page("pages/nl_console.py", title="NL 2 SQL Console", icon="🗄️")
pdf_page = st.Page("pages/pdf_table.py",  title="PDF → Table",      icon="📄")

pg = st.navigation([nl_page, pdf_page])
pg.run()