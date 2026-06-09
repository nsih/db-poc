import streamlit as st
import logging
import re
import pandas as pd
from io import BytesIO

import db_builder
import pdf_extract

logger = logging.getLogger(__name__)

#  상수

_SQL_TYPE_OPTIONS = ["TEXT", "BIGINT", "INT", "DOUBLE", "FLOAT", "TINYINT(1)", "DATE", "DATETIME", "VARCHAR(255)"]


#  세션 초기화

def _reset_pdf_state():
    for k in ("pdf_tables", "pdf_md", "pdf_step", "pdf_table_idx",
              "pdf_col_types", "pdf_table_name", "pending_load", "pdf_merge_mode"):
        st.session_state.pop(k, None)


def _reset_nl_state():
    for k in ("nl_sql", "nl_df", "nl_df_orig", "nl_kind", "nl_pending_commit",
              "nl_target_table", "nl_update_sqls", "nl_update_pending", "nl_edit_gen",
              "nl_save_as"):
        st.session_state.pop(k, None)


#  캐시 리소스

@st.cache_resource
def load_engine():
    return db_builder.get_engine()


#  설정 로드

AI_WORKER_IP   = st.secrets["AI_WORKER_IP"]
AI_WORKER_PORT = st.secrets.get("AI_WORKER_PORT", 1234)
AI_MODEL_NAME  = st.secrets.get("AI_MODEL_NAME", "")
AI_ENDPOINT    = f"http://{AI_WORKER_IP}:{AI_WORKER_PORT}/v1/chat/completions"


#  메인 UI

st.set_page_config(page_title="CSU DB Console", layout="wide")

try:
    engine = load_engine()
except db_builder.DbBuilderError as e:
    st.error(f"DB 연결 실패: {e}")
    st.stop()

mode = st.sidebar.radio(
    "모드 선택",
    ["NL 2 SQL Console", "PDF → Table"],
    on_change=lambda: (_reset_nl_state(), _reset_pdf_state()),
)

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


#
# 모드 1 — NL 2 SQL Console
#

if mode == "NL 2 SQL Console":
    st.title("🗄️ NL 2 SQL Console")
    st.caption("자연어로 질의하면 SQL을 생성합니다. **생성된 SQL을 반드시 확인 후 실행하세요.**")

    #  자연어 입력
    with st.form("nl_form"):
        question = st.text_area("자연어 질의", height=80,
                                placeholder="예) 직원 테이블에서 부서가 IT인 사람 전부 조회해줘")
        submitted = st.form_submit_button("SQL 생성", type="primary")

    if submitted and question.strip():
        _reset_nl_state()
        with st.spinner("스키마 로딩 및 SQL 생성 중..."):
            try:
                schema_prompt = db_builder.get_schema_prompt(engine)
                sql = db_builder.generate_sql(
                    user_question=question,
                    schema_prompt=schema_prompt,
                    model_name=AI_MODEL_NAME,
                    endpoint=AI_ENDPOINT,
                )
                st.session_state["nl_sql"]     = sql
                st.session_state["nl_kind"]    = db_builder.classify_sql(sql)
                st.session_state["nl_sql_gen"] = st.session_state.get("nl_sql_gen", 0) + 1
            except db_builder.DbBuilderError as e:
                st.error(f"SQL 생성 실패: {e}")

    #  생성 SQL 표시 + 실행
    if "nl_sql" in st.session_state:
        sql  = st.session_state["nl_sql"]
        kind = st.session_state["nl_kind"]
        gen  = st.session_state.get("nl_sql_gen", 0)

        st.markdown("#### 생성된 SQL")
        edited_sql = st.text_area("SQL (직접 수정 가능)", value=sql, height=240,
                                  key=f"nl_sql_editor_{gen}")

        # 수정된 SQL을 세션에 반영
        st.session_state["nl_sql"]  = edited_sql
        st.session_state["nl_kind"] = db_builder.classify_sql(edited_sql)
        kind = st.session_state["nl_kind"]

        st.caption(f"구문 분류: **{kind.upper()}**")
        st.markdown("---")

        #  SELECT 경로
        if kind == "select":
            if st.button("▶ 조회 실행", type="primary"):
                for k in ("nl_df", "nl_df_orig", "nl_target_table",
                          "nl_update_sqls", "nl_update_pending"):
                    st.session_state.pop(k, None)
                st.session_state["nl_edit_gen"] = st.session_state.get("nl_edit_gen", 0) + 1
                try:
                    df = db_builder.run_select(engine, edited_sql, limit=20000)
                    st.session_state["nl_df"]      = df
                    st.session_state["nl_df_orig"] = df.copy()
                    m = re.search(r"FROM\s+`?(\w+)`?", edited_sql.rstrip().rstrip(";"), re.IGNORECASE)
                    st.session_state["nl_target_table"] = m.group(1) if m else None
                except db_builder.DbBuilderError as e:
                    st.error(f"조회 실패: {e}")

            if "nl_df" in st.session_state:
                df_orig      = st.session_state["nl_df_orig"]
                target_table = st.session_state.get("nl_target_table")
                edit_gen     = st.session_state.get("nl_edit_gen", 0)

                st.success(f"✅ {len(df_orig)}행 조회됨")

                if target_table:
                    st.caption("셀을 직접 수정한 뒤 **변경 반영** 버튼을 누르세요.")
                    edited_df = st.data_editor(
                        df_orig, use_container_width=True,
                        key=f"nl_editor_{edit_gen}"
                    )
                else:
                    st.caption("조인/집계 쿼리는 편집이 지원되지 않습니다.")
                    edited_df = df_orig
                    st.dataframe(df_orig, use_container_width=True)

                if target_table:
                    btn_col1, btn_col2 = st.columns(2)
                    with btn_col1:
                        if st.button("📝 변경 반영", use_container_width=True):
                            st.session_state.pop("nl_save_as", None)
                            try:
                                update_sqls = db_builder.build_update_sqls(
                                    df_orig, edited_df, target_table
                                )
                                if not update_sqls:
                                    st.info("변경된 셀이 없습니다.")
                                else:
                                    st.session_state["nl_update_sqls"] = update_sqls
                                    st.rerun()
                            except db_builder.DbBuilderError as e:
                                st.error(f"변경 감지 실패: {e}")
                    with btn_col2:
                        if st.button("💾 새 테이블로 저장", use_container_width=True):
                            st.session_state.pop("nl_update_sqls", None)
                            st.session_state["nl_save_as"] = True
                            st.rerun()

                # UPDATE 승인 게이트
                if "nl_update_sqls" in st.session_state:
                    update_sqls = st.session_state["nl_update_sqls"]
                    st.markdown(f"#### 변경 {len(update_sqls)}건 — 생성된 UPDATE SQL")
                    for i, item in enumerate(update_sqls):
                        if item["warning"]:
                            st.warning(f"⚠️ {item['warning']}")
                        st.code(item["sql"], language="sql")

                    st.markdown("---")
                    if "nl_update_pending" not in st.session_state:
                        if st.button("✅ 전체 실행 확정", type="primary"):
                            st.session_state["nl_update_pending"] = True
                            st.rerun()
                    else:
                        st.error("정말 실행하시겠습니까? 되돌릴 수 없습니다.")
                        c1, c2 = st.columns(2)
                        with c1:
                            if st.button("예, 실행", type="primary", use_container_width=True):
                                errors = []
                                for item in update_sqls:
                                    try:
                                        db_builder.run_write(engine, item["sql"], commit=True)
                                    except db_builder.DbBuilderError as e:
                                        errors.append(str(e))
                                if errors:
                                    for err in errors:
                                        st.error(f"실행 실패: {err}")
                                else:
                                    st.success(f"✅ {len(update_sqls)}건 실행 완료")
                                st.session_state.pop("nl_update_sqls", None)
                                st.session_state.pop("nl_update_pending", None)
                                st.rerun()
                        with c2:
                            if st.button("취소", use_container_width=True):
                                st.session_state.pop("nl_update_sqls", None)
                                st.session_state.pop("nl_update_pending", None)
                                st.rerun()

                # 새 테이블로 저장 게이트
                if st.session_state.get("nl_save_as"):
                    st.markdown("#### 새 테이블로 저장")
                    existing_tables = db_builder.list_tables(engine)
                    new_table_name  = st.text_input(
                        "새 테이블명", placeholder="예) ip_table_backup",
                        key="nl_save_as_name"
                    )
                    if_exists = "fail"
                    if new_table_name and new_table_name in existing_tables:
                        st.warning(f"⚠️ `{new_table_name}` 테이블이 이미 존재합니다.")
                        if_exists = st.radio(
                            "처리 방식", ["fail", "replace", "append"],
                            captions=["중단", "덮어쓰기", "이어붙이기"],
                            horizontal=True, key="nl_save_as_ifexists"
                        )
                    s1, s2 = st.columns(2)
                    with s1:
                        if st.button("✅ 저장 실행", type="primary",
                                     disabled=not (new_table_name or "").strip(),
                                     use_container_width=True):
                            try:
                                cnt = db_builder.load_dataframe(
                                    engine, edited_df, new_table_name, if_exists=if_exists
                                )
                                st.success(f"✅ `{new_table_name}` 테이블에 {cnt}행 저장 완료")
                                st.session_state.pop("nl_save_as", None)
                                st.rerun()
                            except db_builder.DbBuilderError as e:
                                st.error(f"저장 실패: {e}")
                    with s2:
                        if st.button("취소", use_container_width=True, key="nl_save_as_cancel"):
                            st.session_state.pop("nl_save_as", None)
                            st.rerun()

        #  DDL / DML 경로
        elif kind in ("ddl", "dml"):
            st.warning("⚠️ 쓰기 작업입니다. SQL을 꼼꼼히 확인하세요.")

            # DDL 파괴적 작업 경고
            if kind == "ddl":
                if re.search(
                    r'\bALTER\s+TABLE\b.+\b(DROP\s+COLUMN|DROP\s+PRIMARY\s+KEY)\b',
                    edited_sql, re.IGNORECASE | re.DOTALL
                ):
                    st.error("컬럼/PK 삭제가 포함된 ALTER입니다. 해당 데이터는 영구 삭제됩니다.")
                elif re.search(r'\bDROP\s+TABLE\b', edited_sql, re.IGNORECASE):
                    st.error("테이블 전체 삭제입니다. 테이블과 데이터가 영구 삭제됩니다.")

            col1, col2 = st.columns(2)

            with col1:
                if kind == "dml":
                    if st.button("🔍 미리보기 (rollback)", use_container_width=True):
                        try:
                            result = db_builder.run_write(engine, edited_sql, commit=False)
                            msg = result.get("message", "")
                            if msg:
                                st.info(msg)
                            else:
                                st.info(f"예상 영향 행 수: {result['rowcount']}행 (미커밋)")
                        except db_builder.DbBuilderError as e:
                            st.error(f"미리보기 실패: {e}")
                else:
                    st.info("DDL은 미리보기가 지원되지 않습니다. SQL을 확인 후 실행하세요.")

            with col2:
                if "nl_pending_commit" not in st.session_state:
                    if st.button("✅ 실행 확정", type="primary", use_container_width=True):
                        st.session_state["nl_pending_commit"] = True
                        st.rerun()
                else:
                    st.error("정말 실행하시겠습니까? 되돌릴 수 없습니다.")
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("예, 실행", type="primary", use_container_width=True):
                            try:
                                result = db_builder.run_write(engine, edited_sql, commit=True)
                                st.success(f"✅ 실행 완료 (영향 행: {result['rowcount']})")
                                st.session_state.pop("nl_pending_commit", None)
                                st.rerun()
                            except db_builder.DbBuilderError as e:
                                st.error(f"실행 실패: {e}")
                                st.session_state.pop("nl_pending_commit", None)
                    with c2:
                        if st.button("취소", use_container_width=True):
                            st.session_state.pop("nl_pending_commit", None)
                            st.rerun()

        else:
            st.error("분류할 수 없는 SQL입니다. 직접 수정 후 재시도하세요.")


#
# 모드 2 — PDF → Table
#

elif mode == "PDF → Table":
    st.title("📄 PDF → Table")
    st.caption("PDF에서 표를 추출해 MySQL 테이블로 적재합니다.")

    step = st.session_state.get("pdf_step", "upload")

    #  Step A: 업로드
    if step == "upload":
        uploaded = st.file_uploader("PDF 선택", type=["pdf"])
        if uploaded:
            with st.spinner("PDF 파싱 중..."):
                try:
                    md_text = pdf_extract.extract_text_from_pdf(BytesIO(uploaded.getvalue()))
                    tables  = db_builder.parse_markdown_tables(md_text)
                    st.session_state["pdf_md"]     = md_text
                    st.session_state["pdf_tables"] = tables
                    st.session_state["pdf_step"]   = "review"
                    st.rerun()
                except pdf_extract.PdfExtractError as e:
                    st.error(f"PDF 파싱 실패: {e}")

    #  Step B: 표 검수
    elif step == "review":
        tables  = st.session_state.get("pdf_tables", [])
        md_text = st.session_state.get("pdf_md", "")

        if not tables:
            st.warning("⚠️ 자동 추출된 표가 없습니다. 아래 마크다운에서 직접 데이터를 확인하세요.")
            with st.expander("추출된 마크다운 원문"):
                st.text_area("마크다운", md_text, height=300)

            st.markdown("#### 빈 그리드로 수동 입력")
            empty_df = pd.DataFrame(columns=["컬럼1", "컬럼2", "컬럼3"])
            edited = st.data_editor(empty_df, num_rows="dynamic",
                                    use_container_width=True, key="manual_grid")
            st.session_state["pdf_tables"] = [edited] if not edited.empty else []

            col1, col2 = st.columns(2)
            with col1:
                if st.button("다음 →", type="primary", disabled=edited.empty):
                    st.session_state["pdf_table_idx"] = 0
                    st.session_state["pdf_step"] = "type_confirm"
                    st.rerun()
            with col2:
                if st.button("처음으로"):
                    _reset_pdf_state()
                    st.rerun()

        else:
            st.success(f"✅ {len(tables)}개 표 추출됨")

            merge_mode = False
            if len(tables) > 1:
                merge_mode = st.toggle(
                    "전체 표를 하나로 통합해서 적재",
                    value=st.session_state.get("pdf_merge_mode", False),
                    help="컬럼 구조가 동일한 표들을 세로로 합칩니다. 컬럼 수/이름이 다르면 빈 값으로 채워집니다.",
                )
                st.session_state["pdf_merge_mode"] = merge_mode

            if merge_mode:
                try:
                    merged_df = pd.concat(tables, ignore_index=True)
                except Exception as e:
                    st.error(f"통합 실패: {e}")
                    merged_df = tables[0]

                drop_cols_m = st.multiselect(
                    "제외할 컬럼 선택", options=list(merged_df.columns),
                    default=[], key="drop_cols_merged",
                )
                if drop_cols_m:
                    merged_df = merged_df.drop(columns=drop_cols_m)

                st.markdown(f"#### 통합 결과 검수 ({len(merged_df)}행 × {len(merged_df.columns)}열, 셀·컬럼명 직접 수정 가능)")
                edited_df = st.data_editor(
                    merged_df, num_rows="dynamic",
                    use_container_width=True, key="editor_merged"
                )
                st.session_state["pdf_tables"]    = [edited_df]
                st.session_state["pdf_table_idx"] = 0

                with st.expander("추출된 마크다운 원문"):
                    st.text_area("마크다운", md_text, height=200)

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("다음: 컬럼 타입 지정 →", type="primary"):
                        st.session_state["pdf_step"] = "type_confirm"
                        st.rerun()
                with col2:
                    if st.button("처음으로"):
                        _reset_pdf_state()
                        st.rerun()

            else:
                idx = st.session_state.get("pdf_table_idx", 0)
                if len(tables) > 1:
                    idx = st.selectbox(
                        "표 선택", range(len(tables)),
                        format_func=lambda i: f"표 {i+1} ({len(tables[i])}행 × {len(tables[i].columns)}열)",
                        index=idx,
                    )
                    st.session_state["pdf_table_idx"] = idx

                drop_cols_s = st.multiselect(
                    "제외할 컬럼 선택", options=list(tables[idx].columns),
                    default=[], key=f"drop_cols_{idx}",
                )
                display_df = tables[idx].drop(columns=drop_cols_s) if drop_cols_s else tables[idx]

                st.markdown(f"#### 표 {idx+1} 검수 (셀·컬럼명 직접 수정 가능)")
                edited_df = st.data_editor(
                    display_df, num_rows="dynamic",
                    use_container_width=True, key=f"editor_{idx}"
                )
                tables[idx] = edited_df
                st.session_state["pdf_tables"] = tables

                with st.expander("추출된 마크다운 원문"):
                    st.text_area("마크다운", md_text, height=200)

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("다음: 컬럼 타입 지정 →", type="primary"):
                        st.session_state["pdf_table_idx"] = idx
                        st.session_state["pdf_step"] = "type_confirm"
                        st.rerun()
                with col2:
                    if st.button("처음으로"):
                        _reset_pdf_state()
                        st.rerun()

    #  Step C: 컬럼 타입 + 테이블명
    elif step == "type_confirm":
        tables = st.session_state.get("pdf_tables", [])
        idx    = st.session_state.get("pdf_table_idx", 0)

        if not tables or idx >= len(tables):
            st.error("표 데이터가 없습니다.")
            _reset_pdf_state()
            st.rerun()

        df = tables[idx]
        st.markdown(f"#### 컬럼 타입 지정 (표 {idx+1})")

        inferred  = db_builder.infer_column_types(df)
        col_types = st.session_state.get("pdf_col_types", inferred)

        updated_types = {}
        cols = st.columns(min(len(df.columns), 4))
        for i, col in enumerate(df.columns):
            with cols[i % len(cols)]:
                selected = st.selectbox(
                    col, _SQL_TYPE_OPTIONS,
                    index=_SQL_TYPE_OPTIONS.index(col_types.get(col, "TEXT"))
                        if col_types.get(col, "TEXT") in _SQL_TYPE_OPTIONS else 0,
                    key=f"type_{col}_{i}"
                )
                updated_types[col] = selected

        st.session_state["pdf_col_types"] = updated_types

        st.markdown("---")
        table_name = st.text_input(
            "테이블명",
            value=st.session_state.get("pdf_table_name", ""),
            placeholder="예) staff_list"
        )

        existing_tables = db_builder.list_tables(engine)
        if_exists = "fail"
        if table_name and table_name in existing_tables:
            st.warning(f"⚠️ `{table_name}` 테이블이 이미 존재합니다.")
            if_exists = st.radio("처리 방식", ["fail", "replace", "append"],
                                 captions=["중단", "덮어쓰기", "이어붙이기"],
                                 horizontal=True)

        col1, col2 = st.columns(2)
        with col1:
            if st.button("← 검수로 돌아가기"):
                st.session_state["pdf_table_name"] = table_name
                st.session_state["pdf_step"] = "review"
                st.rerun()
        with col2:
            if st.button("미리보기 →", type="primary", disabled=not table_name.strip()):
                st.session_state["pdf_table_name"] = table_name
                st.session_state["pending_load"] = {
                    "df": df, "table": table_name, "if_exists": if_exists,
                    "col_types": updated_types,
                }
                st.session_state["pdf_step"] = "confirm_load"
                st.rerun()

    #  Step D: 최종 확인 + 적재
    elif step == "confirm_load":
        pending = st.session_state.get("pending_load", {})
        if not pending:
            st.error("데이터가 없습니다.")
            _reset_pdf_state()
            st.rerun()

        df        = pending["df"]
        table     = pending["table"]
        if_exists = pending["if_exists"]

        st.markdown(f"#### 적재 미리보기: `{table}`")
        st.dataframe(df, use_container_width=True)
        st.caption(f"행 수: {len(df)} | 컬럼: {', '.join(df.columns)}")
        st.caption(f"처리 방식: `{if_exists}`")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("← 타입 지정으로"):
                st.session_state["pdf_step"] = "type_confirm"
                st.rerun()
        with col2:
            if st.button("✅ 적재 실행", type="primary"):
                with st.spinner("적재 중..."):
                    try:
                        cnt = db_builder.load_dataframe(
                            engine, df, table, if_exists=if_exists
                        )
                        st.success(f"✅ `{table}` 테이블에 {cnt}행 적재 완료")
                        _reset_pdf_state()
                        st.balloons()
                    except db_builder.DbBuilderError as e:
                        st.error(f"적재 실패: {e}")