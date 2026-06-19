import streamlit as st
import re
import db_builder
from utils import load_engine, auto_select, reset_nl_state, reset_all

engine = load_engine()

AI_WORKER_IP   = st.secrets["AI_WORKER_IP"]
AI_WORKER_PORT = st.secrets.get("AI_WORKER_PORT", 1234)
AI_MODEL_NAME  = st.secrets.get("AI_MODEL_NAME", "")
AI_ENDPOINT    = f"http://{AI_WORKER_IP}:{AI_WORKER_PORT}/v1/chat/completions"

st.title("🗄️ NL 2 SQL Console")
st.caption("자연어로 질의하면 SQL을 생성합니다. **생성된 SQL을 반드시 확인 후 실행하세요.**")

# 자연어 입력
with st.form("nl_form"):
    question  = st.text_area("자연어 질의", height=80,
                             placeholder="예) 직원 테이블에서 부서가 IT인 사람 전부 조회해줘")
    submitted = st.form_submit_button("SQL 생성", type="primary")

if submitted and question.strip():
    # SQL 새로 생성 시 관련 세션 전체 초기화
    gen = st.session_state.get("nl_sql_gen", 0) + 1
    reset_nl_state()
    st.session_state["nl_sql_gen"] = gen
    with st.spinner("스키마 로딩 및 SQL 생성 중..."):
        try:
            schema_prompt = db_builder.get_schema_prompt(engine)
            sql = db_builder.generate_sql(
                user_question=question,
                schema_prompt=schema_prompt,
                model_name=AI_MODEL_NAME,
                endpoint=AI_ENDPOINT,
            )
            st.session_state["nl_sql"]  = sql
            st.session_state["nl_kind"] = db_builder.classify_sql(sql)
        except db_builder.DbBuilderError as e:
            st.error(f"SQL 생성 실패: {e}")

# nl_done이 True면 완료 처리 후 st.stop()
if st.session_state.get("nl_done"):
    st.success("작업 완료")

    target = st.session_state.get("nl_post_update_target")
    if target:
        try:
            df_after = db_builder.run_select(
                engine, f"SELECT * FROM `{target}`", limit=50
            )
            st.markdown(f"#### 📋 `{target}` 현재 상태 (최대 50행)")
            st.dataframe(df_after, use_container_width=True)
            st.caption(f"{len(df_after)}행 조회됨")
        except db_builder.DbBuilderError as e:
            st.warning(f"자동 조회 실패: {e}")

    if st.button("🔄 다음 작업 실행", type="primary"):
        reset_all()
        st.rerun()
    st.stop()

# 생성 SQL 표시 + 실행
if "nl_sql" not in st.session_state:
    st.stop()

sql  = st.session_state["nl_sql"]
kind = st.session_state["nl_kind"]
gen  = st.session_state.get("nl_sql_gen", 0)

st.markdown("#### 생성된 SQL")
edited_sql = st.text_area("SQL (직접 수정 가능)", value=sql, height=200,
                          key=f"nl_sql_editor_{gen}")

# 수정된 SQL 세션 반영
st.session_state["nl_sql"]  = edited_sql
st.session_state["nl_kind"] = db_builder.classify_sql(edited_sql)
kind = st.session_state["nl_kind"]

st.caption(f"구문 분류: **{kind.upper()}**")
st.markdown("---")

# SELECT 경로
if kind == "select":
    if st.button("▶ 조회 실행", type="primary"):
        for k in ("nl_df", "nl_df_orig", "nl_target_table",
                  "nl_update_sqls", "nl_update_pending", "nl_save_as",
                  "nl_target_is_view"):
            st.session_state.pop(k, None)
        st.session_state["nl_edit_gen"] = st.session_state.get("nl_edit_gen", 0) + 1
        try:
            df = db_builder.run_select(engine, edited_sql, limit=20000)
            st.session_state["nl_df"]      = df
            st.session_state["nl_df_orig"] = df.copy()
            m = re.search(r"FROM\s+`?(\w+)`?", edited_sql.rstrip().rstrip(";"), re.IGNORECASE)
            target_name = m.group(1) if m else None
            st.session_state["nl_target_table"] = target_name

            # 뷰 여부 판별
            try:
                current_views = db_builder.list_views(engine)
            except db_builder.DbBuilderError:
                current_views = []
            st.session_state["nl_target_is_view"] = (target_name in current_views)

        except db_builder.DbBuilderError as e:
            st.error(f"조회 실패: {e}")

    if "nl_df" not in st.session_state:
        st.stop()

    df_orig      = st.session_state["nl_df_orig"]
    target_table = st.session_state.get("nl_target_table")
    is_view      = st.session_state.get("nl_target_is_view", False)
    edit_gen     = st.session_state.get("nl_edit_gen", 0)

    st.success(f"✅ {len(df_orig)}행 조회됨")

    if is_view:
        # 뷰 — 읽기 전용 표시
        st.info("👁 뷰 조회 결과입니다. 뷰는 편집할 수 없습니다.")
        st.dataframe(df_orig, use_container_width=True)

    elif target_table:
        st.caption("수정하려면 셀을 수정하고 **변경 반영** 버튼을 누르세요.")
        edited_df = st.data_editor(
            df_orig, use_container_width=True,
            key=f"nl_editor_{edit_gen}"
        )
    else:
        st.caption("조인/집계 쿼리는 편집이 지원되지 않습니다.")
        edited_df = df_orig
        st.dataframe(df_orig, use_container_width=True)

    # 편집 버튼은 일반 테이블 대상일 때만 표시
    if target_table and not is_view:
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
        for item in update_sqls:
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
                    st.session_state.pop("nl_update_sqls", None)
                    st.session_state.pop("nl_update_pending", None)
                    if errors:
                        for err in errors:
                            st.error(f"실행 실패: {err}")
                    else:
                        st.session_state["nl_done"] = True
                        st.session_state["nl_post_update_target"] = target_table
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
            if_exists = st.radio("처리 방식", ["fail", "replace", "append"],
                                 captions=["중단", "덮어쓰기", "이어붙이기"],
                                 horizontal=True, key="nl_save_as_ifexists")
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

# DDL / DML 경로
elif kind in ("ddl", "dml"):
    st.warning("⚠️ 쓰기 작업입니다. SQL을 꼼꼼히 확인하세요.")

    if kind == "ddl":
        if re.search(
            r'\bDROP\s+TABLE\b', edited_sql, re.IGNORECASE
        ):
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
            if st.button("🔍 사전 검사", use_container_width=True):
                try:
                    preview = db_builder.preview_ddl(engine, edited_sql)
                    st.session_state["nl_ddl_preview"] = preview
                except db_builder.DbBuilderError as e:
                    st.error(f"검사 실패: {e}")

            if "nl_ddl_preview" in st.session_state:
                preview = st.session_state["nl_ddl_preview"]
                st.caption(f"구문 유형: {preview['type']}")
                if preview.get("table"):
                    st.caption(f"대상 테이블: {preview['table']}")
                for f in preview.get("findings", []):
                    if f["level"] == "error":
                        st.error(f["msg"])
                    elif f["level"] == "warning":
                        st.warning(f["msg"])
                    else:
                        st.caption(f["msg"])

    with col2:
        if "nl_pending_commit" not in st.session_state:
            if st.button("✅ 실행 확정", type="primary", use_container_width=True):
                st.session_state["nl_pending_commit"] = True
                st.session_state.pop("nl_ddl_preview", None)
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
                        st.session_state["nl_done"] = True
                        auto_select(engine, edited_sql)
                        st.rerun()
                    except db_builder.DbBuilderError as e:
                        st.error(f"실행 실패: {e}")
                        st.session_state.pop("nl_pending_commit", None)
            with c2:
                if st.button("취소", use_container_width=True):
                    st.session_state.pop("nl_pending_commit", None)
                    st.session_state.pop("nl_ddl_preview", None)
                    st.rerun()

else:
    st.error("분류할 수 없는 SQL입니다. 직접 수정 후 재시도하세요.")