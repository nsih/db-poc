import streamlit as st
import pandas as pd
from io import BytesIO
import db_builder
import pdf_extract
from utils import load_engine, reset_pdf_state, reset_all

engine = load_engine()

_SQL_TYPE_OPTIONS = ["TEXT", "BIGINT", "INT", "DOUBLE", "FLOAT",
                     "TINYINT(1)", "DATE", "DATETIME", "VARCHAR(255)"]

st.title("파일 → Table")
st.caption("PDF, CSV, Excel 파일에서 표를 추출해 MySQL 테이블로 적재합니다.")

step = st.session_state.get("pdf_step", "upload")

# Step A: 업로드

if step == "upload":
    uploaded = st.file_uploader("파일 선택", type=["pdf", "csv", "xlsx", "xls"])
    if uploaded:
        ext = uploaded.name.rsplit(".", 1)[-1].lower()

        # CSV / Excel — pandas로 바로 읽어 Step B 진입
        if ext in ("csv", "xlsx", "xls"):
            with st.spinner("파일 파싱 중..."):
                try:
                    if ext == "csv":
                        # 인코딩 자동 감지 — utf-8 실패 시 cp949 재시도
                        try:
                            df = pd.read_csv(BytesIO(uploaded.getvalue()), encoding="utf-8")
                        except UnicodeDecodeError:
                            df = pd.read_csv(BytesIO(uploaded.getvalue()), encoding="cp949")
                    else:
                        df = pd.read_excel(BytesIO(uploaded.getvalue()))

                    # Unnamed 컬럼명 정리
                    df.columns = [
                        str(c) if not str(c).startswith("Unnamed") else f"컬럼{i+1}"
                        for i, c in enumerate(df.columns)
                    ]
                    df = df.astype(str).replace("nan", "")

                    st.session_state["pdf_md"]     = ""
                    st.session_state["pdf_tables"] = [df]
                    st.session_state["pdf_step"]   = "review"
                    st.rerun()
                except Exception as e:
                    st.error(f"파일 파싱 실패: {e}")

        # PDF — 기존 파이프라인
        else:
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

# Step B: 표 검수

elif step == "review":
    tables  = st.session_state.get("pdf_tables", [])
    md_text = st.session_state.get("pdf_md", "")

    if not tables:
        st.warning("⚠️ 자동 추출된 표가 없습니다. 아래 마크다운에서 직접 데이터를 확인하세요.")
        with st.expander("추출된 마크다운 원문"):
            st.text_area("마크다운", md_text, height=300)

        st.markdown("#### 빈 그리드로 수동 입력")
        empty_df = pd.DataFrame(columns=["컬럼1", "컬럼2", "컬럼3"])
        edited   = st.data_editor(empty_df, num_rows="dynamic",
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
                reset_pdf_state()
                st.rerun()

    else:
        st.success(f"{len(tables)}개 표 추출됨")

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

            if md_text:
                with st.expander("추출된 마크다운 원문"):
                    st.text_area("마크다운", md_text, height=200)

            col1, col2 = st.columns(2)
            with col1:
                if st.button("다음: 컬럼 타입 지정 →", type="primary"):
                    st.session_state["pdf_step"] = "type_confirm"
                    st.rerun()
            with col2:
                if st.button("처음으로"):
                    reset_pdf_state()
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

            if md_text:
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
                    reset_pdf_state()
                    st.rerun()

# Step C: 컬럼 타입 + 테이블명

elif step == "type_confirm":
    tables = st.session_state.get("pdf_tables", [])
    idx    = st.session_state.get("pdf_table_idx", 0)

    if not tables or idx >= len(tables):
        st.error("표 데이터가 없습니다.")
        reset_pdf_state()
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

# Step D: 최종 확인 + 적재 

elif step == "confirm_load":
    pending = st.session_state.get("pending_load", {})
    if not pending:
        st.error("데이터가 없습니다.")
        reset_pdf_state()
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
        if st.button("적재 실행", type="primary"):
            with st.spinner("적재 중..."):
                try:
                    cnt = db_builder.load_dataframe(
                        engine, df, table, if_exists=if_exists
                    )
                    st.success(f"`{table}` 테이블에 {cnt}행 적재 완료")
                    try:
                        df_after = db_builder.run_select(
                            engine, f"SELECT * FROM `{table}`", limit=20000
                        )
                        st.markdown(f"#### `{table}` 적재 결과 (최대 20000행)")
                        st.dataframe(df_after, use_container_width=True)
                        st.caption(f"{len(df_after)}행 조회됨")
                    except db_builder.DbBuilderError as e:
                        st.warning(f"자동 조회 실패: {e}")
                    reset_all()
                    st.balloons()
                except db_builder.DbBuilderError as e:
                    st.error(f"적재 실패: {e}")