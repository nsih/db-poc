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
    st.caption("테이블 목록")
    try:
        tables = db_builder.list_tables(engine)
        if tables:
            for t in tables:
                if st.button(f"• {t}", key=f"tbl_btn_{t}", use_container_width=True):
                    st.session_state["quick_view_table"] = t
        else:
            st.caption("(테이블 없음)")
    except Exception:
        st.caption("조회 실패")
        

selected = st.session_state.get("quick_view_table")
if selected:
    with st.container(border=True):
        col1, col2 = st.columns([6, 1])
        with col1:
            st.markdown(f"#### `{selected}` \n Table preview (최대 100행)")
        with col2:
            if st.button("닫기", use_container_width=True, key="close_quick_view"):
                st.session_state.pop("quick_view_table", None)
                st.rerun()
        try:
            df = db_builder.run_select(engine, f"SELECT * FROM `{selected}`", limit=100)
            st.dataframe(df, use_container_width=True)
            st.caption(f"{len(df)}행 조회됨")
        except db_builder.DbBuilderError as e:
            st.error(f"조회 실패: {e}")

    st.markdown("---")

nl_page   = st.Page("pages/nl_console.py", title="NL 2 SQL Console")
file_page = st.Page("pages/file_table.py", title="파일 → Table")

pg = st.navigation([nl_page, file_page])
pg.run()