import re
import streamlit as st
import db_builder


@st.cache_resource
def load_engine():
    return db_builder.get_engine()


def extract_target_table(sql: str) -> str | None:
    if re.search(r'\bDROP\s+TABLE\b', sql, re.IGNORECASE):
        return None

    m = re.search(
        r'\b(?:INTO|TABLE|FROM|UPDATE)\s+`?(\w+)`?',
        sql, re.IGNORECASE
    )
    return m.group(1) if m else None


def auto_select(engine, sql: str) -> None:
    table = extract_target_table(sql)
    if not table:
        return

    try:
        df = db_builder.run_select(engine, f"SELECT * FROM `{table}`", limit=50)
        st.markdown(f"#### 📋 `{table}` 현재 상태 (최대 50행)")
        st.dataframe(df, use_container_width=True)
        st.caption(f"{len(df)}행 조회됨")
    except db_builder.DbBuilderError as e:
        st.warning(f"자동 조회 실패: {e}")


def reset_nl_state() -> None:
    for k in ("nl_sql", "nl_df", "nl_df_orig", "nl_kind", "nl_pending_commit",
              "nl_target_table", "nl_update_sqls", "nl_update_pending",
              "nl_edit_gen", "nl_sql_gen", "nl_save_as", "nl_done",
              "nl_ddl_preview", "nl_post_update_target"):
        st.session_state.pop(k, None)


def reset_pdf_state() -> None:
    for k in ("pdf_tables", "pdf_md", "pdf_step", "pdf_table_idx",
              "pdf_col_types", "pdf_table_name", "pending_load", "pdf_merge_mode"):
        st.session_state.pop(k, None)


def reset_all() -> None:
    reset_nl_state()
    reset_pdf_state()
    st.session_state.pop("quick_view_table", None)