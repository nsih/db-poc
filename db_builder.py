# db_builder.py
# 순수 로직 모듈 — Streamlit을 import하지 않는다.
# UI(위젯·버튼·상태)는 전부 db_app.py가 담당.

import logging
import re
import tomllib
from pathlib import Path

import pandas as pd
import requests
import sqlparse
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# 예외

class DbBuilderError(Exception):
    pass


# 설정 로드

def _load_secrets() -> dict:
    path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)


# 연결

def get_engine() -> Engine:
    """secrets.toml의 DB 개별 파라미터로 SQLAlchemy 엔진 생성.
    URL 방식은 특수문자 패스워드에서 파싱 오류가 발생하므로
    create_engine URL + connect_args 방식으로 우회한다.
    캐싱은 호출측(db_app)이 @st.cache_resource로 담당."""
    secrets = _load_secrets()
    host     = secrets.get("DB_HOST", "127.0.0.1")
    port     = int(secrets.get("DB_PORT", 3306))
    name     = secrets.get("DB_NAME", "csu_db")
    user     = secrets.get("DB_USER", "csu_admin")
    password = secrets.get("DB_PASSWORD", "")

    try:
        engine = create_engine(
            f"mysql+pymysql://{user}@{host}:{port}/{name}",
            connect_args={"password": password},
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("DB 연결 성공")
        return engine
    except Exception as e:
        raise DbBuilderError(f"DB 연결 실패: {e}")


# Introspection

def list_tables(engine: Engine) -> list[str]:
    try:
        return inspect(engine).get_table_names()
    except Exception as e:
        raise DbBuilderError(f"테이블 목록 조회 실패: {e}")


def get_schema(engine: Engine, table: str) -> dict:
    try:
        insp = inspect(engine)
        return {
            "columns":      insp.get_columns(table),
            "pk":           insp.get_pk_constraint(table),
            "foreign_keys": insp.get_foreign_keys(table),
        }
    except Exception as e:
        raise DbBuilderError(f"스키마 조회 실패 ({table}): {e}")


def get_schema_prompt(engine: Engine,
                      tables: list[str] | None = None,
                      sample_rows: int = 3) -> str:
    if tables is None:
        tables = list_tables(engine)

    parts: list[str] = []
    for table in tables:
        schema = get_schema(engine, table)

        col_defs = []
        pk_cols = set(schema["pk"].get("constrained_columns", []))
        for col in schema["columns"]:
            nullable = "" if col["nullable"] else " NOT NULL"
            pk_mark  = " PRIMARY KEY" if col["name"] in pk_cols else ""
            col_defs.append(f"  {col['name']} {col['type']}{nullable}{pk_mark}")

        for fk in schema["foreign_keys"]:
            ref_table  = fk["referred_table"]
            local_cols = ", ".join(fk["constrained_columns"])
            ref_cols   = ", ".join(fk["referred_columns"])
            col_defs.append(f"  FOREIGN KEY ({local_cols}) REFERENCES {ref_table}({ref_cols})")

        create_stmt = f"CREATE TABLE {table} (\n" + ",\n".join(col_defs) + "\n);"
        parts.append(create_stmt)

        if sample_rows > 0:
            try:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(f"SELECT * FROM `{table}` LIMIT :n"),
                        {"n": sample_rows}
                    ).fetchall()
                if rows:
                    headers = [col["name"] for col in schema["columns"]]
                    sample_lines = ["-- 샘플 데이터:"]
                    sample_lines.append("-- " + " | ".join(headers))
                    for row in rows:
                        sample_lines.append("-- " + " | ".join(str(v) for v in row))
                    parts.append("\n".join(sample_lines))
            except Exception as e:
                logger.warning(f"샘플 행 조회 실패 ({table}): {e}")

        parts.append("")

    return "\n".join(parts).strip()


# SQL 가드

_DANGEROUS_PATTERNS = re.compile(
    r'\b(DROP\s+DATABASE|DROP\s+SCHEMA|TRUNCATE|'
    r'DROP\s+USER|GRANT|REVOKE|SHUTDOWN|'
    r'LOAD\s+DATA|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b',
    re.IGNORECASE,
)

_SHOW_RE = re.compile(r'^\s*(SHOW|DESCRIBE|DESC|EXPLAIN)\b', re.IGNORECASE)


def classify_sql(sql: str) -> str:
    """첫 구문 verb 판별 → 'select' | 'ddl' | 'dml' | 'unknown'."""
    sql = sql.strip()
    if not sql:
        return "unknown"

    if _SHOW_RE.match(sql):
        return "select"

    try:
        parsed = sqlparse.parse(sql)
        if not parsed:
            return "unknown"
        stmt  = parsed[0]
        stype = stmt.get_type()
        if stype == "SELECT":
            return "select"
        if stype in ("CREATE", "ALTER", "DROP", "RENAME"):
            return "ddl"
        if stype in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            return "dml"
        return "unknown"
    except Exception:
        return "unknown"


def guard_sql(sql: str, allow_write: bool) -> None:
    sql = sql.strip()
    if not sql:
        raise DbBuilderError("SQL이 비어 있습니다.")

    stmts = [s for s in sqlparse.split(sql) if s.strip()]
    if len(stmts) > 1:
        raise DbBuilderError("복수 SQL 문장은 허용되지 않습니다. 한 번에 하나씩 실행하세요.")

    if _DANGEROUS_PATTERNS.search(sql):
        raise DbBuilderError("허용되지 않는 구문이 포함되어 있습니다 (DROP DATABASE / TRUNCATE 등).")

    if not allow_write:
        kind = classify_sql(sql)
        if kind != "select":
            raise DbBuilderError("조회 경로에서는 SELECT / SHOW / DESCRIBE / EXPLAIN만 허용됩니다.")

def add_limit(sql: str, limit: int = 20000) -> str:
    """SELECT에 LIMIT이 없으면 강제 주입. SHOW / DESCRIBE / EXPLAIN은 스킵."""
    if classify_sql(sql) != "select":
        return sql
    if _SHOW_RE.match(sql.strip()):
        return sql
    if re.search(r'\bLIMIT\b', sql, re.IGNORECASE):
        return sql
    sql_stripped = sql.rstrip().rstrip(";")
    return f"{sql_stripped} LIMIT {limit}"


# 실행

def run_select(engine: Engine, sql: str, limit: int = 20000) -> pd.DataFrame:
    guard_sql(sql, allow_write=False)
    safe_sql = add_limit(sql, limit)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(safe_sql))
            rows = result.fetchall()
            cols = list(result.keys())
        return pd.DataFrame(rows, columns=cols)
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"SELECT 실행 실패: {e}")


def run_write(engine: Engine, sql: str, commit: bool = False) -> dict:
    guard_sql(sql, allow_write=True)

    kind = classify_sql(sql)

    if kind == "ddl" and not commit:
        return {"rowcount": -1, "committed": False,
                "message": "DDL은 미리보기가 지원되지 않습니다. SQL을 확인 후 실행하세요."}

    try:
        if not commit:
            # rollback 경로 — engine.connect()로 트랜잭션을 커밋하지 않고 종료
            with engine.connect() as conn:
                result   = conn.execute(text(sql))
                rowcount = result.rowcount if result.rowcount is not None else -1
            return {"rowcount": rowcount, "committed": False}

        with engine.begin() as conn:
            result   = conn.execute(text(sql))
            rowcount = result.rowcount if result.rowcount is not None else -1
            return {"rowcount": rowcount, "committed": True}
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"쓰기 실행 실패: {e}")


# LLM 호출 (NL2SQL)

_NL2SQL_SYSTEM = (
    "당신은 MySQL 전문가입니다. "
    "주어진 스키마로 자연어 질의에 대한 MySQL 쿼리를 반환한다."
    "컬럼명에 공백( ) 대신 언더바(_)를 대신 사용한다"
    "별칭(AS 뒤에 오는 이름)에는 절대 공백을 사용하지 않는다. "
    "사용자 질의에 공백이 포함된 단어가 있어도, 별칭에는 반드시 언더바(_)로 변환해 적용한다. "
    "예시: 사용자가 '단말기 개수'라고 표현해도 별칭은 AS 단말기_개수 로 작성한다."
    "주석 없이 SQL 쿼리만 출력한다."
    "세미콜론은 문장 끝에 한 번만 붙인다"
)

_ALIAS_STOP_WORDS = {"FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT"}

def _quote_unquoted_alias_with_space(sql: str) -> str:
    pattern = re.compile(r'\b(AS)\s+([^,;]+?)(?=[,;]|$)', re.IGNORECASE)

    def _repl(m: re.Match) -> str:
        as_kw = m.group(1)
        rest  = m.group(2)
        tokens = rest.split()

        collected: list[str] = []
        for tok in tokens:
            if tok.upper() in _ALIAS_STOP_WORDS:
                break
            collected.append(tok)

        if len(collected) < 2:
            return m.group(0)

        alias     = " ".join(collected)
        remainder = rest[len(alias):]
        return f"{as_kw} `{alias}`{remainder}"

    return pattern.sub(_repl, sql)


def generate_sql(user_question: str, schema_prompt: str,
                 model_name: str, endpoint: str) -> str:
    prompt = (
        f"/no_think\n\n"
        f"[DB 스키마]\n{schema_prompt}\n\n"
        f"[질의]\n{user_question}"
    )

    payload: dict = {
        "messages": [
            {"role": "system", "content": _NL2SQL_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        "stream":      False,
        "temperature": 0.1,
        "max_tokens":  512,
    }
    if model_name:
        payload["model"] = model_name

    try:
        res = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=(10, 120),
        )
        if res.status_code != 200:
            raise DbBuilderError(f"LM Studio 응답 오류: {res.status_code} - {res.text}")
        raw = res.json()["choices"][0]["message"]["content"]
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"LM Studio 통신 실패: {e}")

    sql = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
    sql = re.sub(r'```(?:sql)?', '', sql, flags=re.IGNORECASE)
    sql = sql.replace('```', '').strip()

    if not sql:
        raise DbBuilderError("LLM이 SQL을 생성하지 못했습니다.")

    sql = _quote_unquoted_alias_with_space(sql)

    return sql


# PDF 표 → 적재

def parse_markdown_tables(md_text: str) -> list[pd.DataFrame]:
    results: list[pd.DataFrame] = []

    table_pattern = re.compile(
        r'(\|[^\n]+\|\n\|[-:| ]+\|\n(?:\|[^\n]+\|\n?)+)',
        re.MULTILINE,
    )
    matches = table_pattern.findall(md_text)

    def _clean_cell(s: str) -> str:
        s = re.sub(r'\*{1,3}', '', s)
        s = re.sub(r'_{1,3}', '', s)
        return s.strip()

    for match in matches:
        try:
            lines = [l.strip() for l in match.strip().split('\n') if l.strip()]
            if len(lines) < 2:
                continue

            headers    = [_clean_cell(h) for h in lines[0].split('|') if h.strip()]
            data_lines = lines[2:]

            rows = []
            for line in data_lines:
                cells = [_clean_cell(c) for c in line.split('|') if c != '']
                if len(cells) < len(headers):
                    cells += [''] * (len(headers) - len(cells))
                elif len(cells) > len(headers):
                    cells = cells[:len(headers)]
                rows.append(cells)

            if not rows:
                continue

            df = pd.DataFrame(rows, columns=headers)
            results.append(df)
        except Exception as e:
            logger.warning(f"표 파싱 실패 (스킵): {e}")
            continue

    logger.info(f"parse_markdown_tables: {len(results)}개 표 추출")
    return results


_DTYPE_MAP: dict[str, str] = {
    "int64":   "BIGINT",
    "int32":   "INT",
    "float64": "DOUBLE",
    "float32": "FLOAT",
    "bool":    "TINYINT(1)",
    "object":  "TEXT",
    "string":  "TEXT",
}


def infer_column_types(df: pd.DataFrame) -> dict[str, str]:
    result: dict[str, str] = {}
    for col in df.columns:
        converted = pd.to_numeric(df[col], errors='coerce')
        if converted.notna().sum() > 0 and df[col].notna().sum() > 0:
            if (converted.dropna() % 1 == 0).all():
                result[col] = "BIGINT"
            else:
                result[col] = "DOUBLE"
            continue
        dtype_str = str(df[col].dtype)
        result[col] = _DTYPE_MAP.get(dtype_str, "TEXT")
    return result


def load_dataframe(engine: Engine, df: pd.DataFrame,
                   table: str, if_exists: str = "fail") -> int:
    if df.empty:
        raise DbBuilderError("적재할 데이터가 없습니다 (DataFrame이 비어 있음).")

    df = df.replace({"": None, "nan": None, "None": None})

    try:
        written = df.to_sql(
            name=table,
            con=engine,
            if_exists=if_exists,
            index=False,
            chunksize=500,
        )
        count = written if written is not None else len(df)
        logger.info(f"load_dataframe 완료: {table} {count}행")
        return count
    except Exception as e:
        raise DbBuilderError(f"테이블 적재 실패 ({table}): {e}")


# 인라인 편집 → UPDATE 생성

def diff_dataframes(original: pd.DataFrame,
                    edited: pd.DataFrame) -> pd.DataFrame:
    if original.shape != edited.shape:
        return pd.DataFrame()
    changed_mask = ~(original.astype(str) == edited.astype(str)).all(axis=1)
    return edited[changed_mask]


def build_update_sqls(original: pd.DataFrame,
                      edited: pd.DataFrame,
                      table: str) -> list[dict]:
    if list(original.columns) != list(edited.columns):
        raise DbBuilderError("원본과 편집본의 컬럼 구조가 다릅니다.")

    if original.shape[0] != edited.shape[0]:
        raise DbBuilderError("행 수가 다릅니다. 행 추가/삭제는 지원하지 않습니다.")

    cols    = list(original.columns)
    results = []

    for idx in original.index:
        orig_row = original.loc[idx]
        edit_row = edited.loc[idx]

        if (orig_row.astype(str) == edit_row.astype(str)).all():
            continue

        set_parts = []
        for col in cols:
            if str(orig_row[col]) != str(edit_row[col]):
                val = edit_row[col]
                if pd.isna(val):
                    set_parts.append(f"`{col}` = NULL")
                else:
                    escaped = str(val).replace("'", "''")
                    set_parts.append(f"`{col}` = '{escaped}'")

        where_parts = []
        for col in cols:
            val = orig_row[col]
            if pd.isna(val):
                where_parts.append(f"`{col}` IS NULL")
            else:
                escaped = str(val).replace("'", "''")
                where_parts.append(f"`{col}` = '{escaped}'")

        sql = (f"UPDATE `{table}` "
               f"SET {', '.join(set_parts)} "
               f"WHERE {' AND '.join(where_parts)}")

        dup_count = (original.astype(str) == orig_row.astype(str)).all(axis=1).sum()
        warning   = (f"원본에 동일한 행이 {dup_count}개 존재 — WHERE 조건이 복수 행에 적용될 수 있습니다."
                     if dup_count > 1 else None)

        results.append({"sql": sql, "warning": warning})

    return results


# DDL 정적 검사

def preview_ddl(engine: Engine, sql: str) -> dict:
    guard_sql(sql, allow_write=True)
    if classify_sql(sql) != "ddl":
        raise DbBuilderError("DDL이 아닙니다.")

    findings: list[dict] = []
    result = {"type": "알 수 없음", "table": None, "findings": findings}

    existing_tables = list_tables(engine)

    m = re.match(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?', sql, re.IGNORECASE)
    if m:
        table          = m.group(1)
        result["type"]  = "CREATE TABLE"
        result["table"] = table
        if table in existing_tables:
            if re.search(r'IF\s+NOT\s+EXISTS', sql, re.IGNORECASE):
                findings.append({"level": "warning",
                                  "msg": f"테이블 {table} 이미 존재 — IF NOT EXISTS로 인해 스킵됩니다"})
            else:
                findings.append({"level": "error",
                                  "msg": f"테이블 {table} 이미 존재 — 실행 시 오류 발생"})
        else:
            findings.append({"level": "info", "msg": f"테이블 {table} 신규 생성"})
        return result

    m = re.match(r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?`?(\w+)`?', sql, re.IGNORECASE)
    if m:
        table          = m.group(1)
        result["type"]  = "DROP TABLE"
        result["table"] = table
        if table not in existing_tables:
            findings.append({"level": "error",
                              "msg": f"테이블 {table} 존재하지 않음 — 실행 시 오류 발생"})
        else:
            findings.append({"level": "warning",
                              "msg": f"테이블 {table} 및 모든 데이터 영구 삭제"})
        return result

    m = re.match(r'ALTER\s+TABLE\s+`?(\w+)`?', sql, re.IGNORECASE)
    if m:
        table          = m.group(1)
        result["type"]  = "ALTER TABLE"
        result["table"] = table

        if table not in existing_tables:
            findings.append({"level": "error",
                              "msg": f"테이블 {table} 존재하지 않음 — 실행 시 오류 발생"})
            return result

        findings.append({"level": "info", "msg": f"대상 테이블 {table} 존재함"})

        try:
            existing_cols = {c["name"] for c in inspect(engine).get_columns(table)}
        except Exception:
            existing_cols = set()

        for col_m in re.finditer(r'ADD\s+(?:COLUMN\s+)?`?(\w+)`?', sql, re.IGNORECASE):
            col = col_m.group(1)
            if col in existing_cols:
                findings.append({"level": "error",
                                  "msg": f"ADD COLUMN {col} — 이미 존재하는 컬럼"})
            else:
                findings.append({"level": "info",
                                  "msg": f"ADD COLUMN {col} — 신규 추가"})

        for col_m in re.finditer(r'DROP\s+(?:COLUMN\s+)?`?(\w+)`?', sql, re.IGNORECASE):
            col = col_m.group(1)
            if col not in existing_cols:
                findings.append({"level": "error",
                                  "msg": f"DROP COLUMN {col} — 존재하지 않는 컬럼"})
            else:
                findings.append({"level": "warning",
                                  "msg": f"DROP COLUMN {col} — 삭제 후 복구 불가"})

        for col_m in re.finditer(
            r'RENAME\s+COLUMN\s+`?(\w+)`?\s+TO\s+`?(\w+)`?', sql, re.IGNORECASE
        ):
            old, new = col_m.group(1), col_m.group(2)
            if old not in existing_cols:
                findings.append({"level": "error",
                                  "msg": f"RENAME COLUMN {old} — 존재하지 않는 컬럼"})
            else:
                findings.append({"level": "info",
                                  "msg": f"RENAME COLUMN {old} → {new}"})

        return result

    findings.append({"level": "info", "msg": "세부 분석이 지원되지 않는 DDL 구문입니다."})
    return result