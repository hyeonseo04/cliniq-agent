"""Oracle 커넥션 관리. 리포 경로: src/core/db.py

현재 **통합DB만 OMOP CDM v5.3 구조**이며 지원 대상이다.
전처리DB·연계DB는 구조가 확정되면 각자의 schema JSON과 함께 추가한다.
python-oracledb thin 모드를 사용하므로 Instant Client 설치가 불필요하다.

안전 원칙:
  - 읽기 전용 계정 사용 권장
  - SELECT / EXPLAIN PLAN 외의 문장은 코드에서 차단
  - 타임아웃 설정
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import config
from models.common import DB_KO, TargetDB, assert_supported

log = logging.getLogger(__name__)

# 허용 문장: SELECT 또는 EXPLAIN PLAN 으로 시작하는 단일 문장만
_ALLOWED = re.compile(r"^\s*(SELECT|EXPLAIN\s+PLAN)\b", re.IGNORECASE)


class UnsafeSQLError(RuntimeError):
    pass


def assert_read_only(sql: str) -> None:
    """DDL/DML 차단. 세미콜론으로 문장을 이어붙이는 시도도 막는다."""
    stripped = sql.strip().rstrip(";")
    if not _ALLOWED.match(stripped):
        raise UnsafeSQLError(f"허용되지 않은 SQL: {stripped[:60]}")
    if ";" in stripped:
        raise UnsafeSQLError("여러 문장을 한 번에 실행할 수 없다")


class OracleDB:
    def __init__(self, target_db: TargetDB = TargetDB.INTEGRATED):
        assert_supported(target_db)  # 미지원 DB 차단
        self.target_db = target_db
        self.dsn = config.ORACLE_DSN.get(target_db.value, "")
        if not self.dsn:
            raise ValueError(
                f"{DB_KO.get(target_db, target_db.value)} DSN이 설정되지 않았다. "
                f".env의 ORACLE_DSN_{target_db.value.upper()}를 확인하라."
            )

    @contextmanager
    def connect(self):
        import oracledb  # 지연 import

        conn = oracledb.connect(
            user=config.ORACLE_USER,
            password=config.ORACLE_PASSWORD,
            dsn=self.dsn,
        )
        conn.call_timeout = config.SQL_TIMEOUT_S * 1000  # ms
        try:
            yield conn
        finally:
            conn.close()

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM dual")
                return cur.fetchone() is not None
        except Exception as e:  # noqa: BLE001
            log.error("DB 연결 실패 (%s): %s", self.target_db.value, e)
            return False