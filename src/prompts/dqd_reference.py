"""DQD(OHDSI Data Quality Dashboard) 표준 지표 참고 자료.
리포 경로: src/prompts/dqd_reference.py

출처: OMOP_CDMv5.3_Check_Descriptions.csv (27종)
용도: Rule Generation 프롬프트에 주입.
     요청에 해당하는 표준 지표가 있으면 그 정의를 따라 인스턴스화(DQD 변환),
     없으면 스키마를 근거로 자유 생성(LLM-DQR 방식).

레코드 단위로 표현 불가능한 체크(cdmTable 존재 여부 등)와
CONCEPT 레벨 체크(concept_ancestor JOIN 필요)는 제외했다.
"""

DQD_REFERENCE = """## 참고: 표준 데이터 품질 지표 (OHDSI DQD)

아래는 임상 데이터 품질 평가의 국제 표준 검사 유형이다.
**요청에 해당하는 유형이 있으면 반드시 그 정의와 품질 차원을 따르고,
규칙 이름 끝에 `[지표: 지표ID]`를 표기하라.**
해당하는 유형이 없으면 스키마를 근거로 직접 설계하되, 아래 정의 방식을 참고하라.

### 완전성 (completeness)
- `isRequired`: NOT NULL이어야 하는 컬럼에 NULL이 있는 레코드
- `measureValueCompleteness`: 특정 컬럼이 NULL인 레코드 (필수 여부 무관)
- `standardConceptRecordCompleteness`: 표준 concept 컬럼(*_concept_id)의 값이 0인 레코드
  (0 = 표준 용어 매핑 실패)
- `sourceConceptRecordCompleteness`: 원천 concept 컬럼(*_source_concept_id)의 값이 0인 레코드
- `sourceValueCompleteness`: 원천 값(*_source_value)이 0으로 매핑된 레코드

### 정합성 (conformity)
- `isForeignKey`: FK 컬럼의 값이 참조 테이블에 존재하지 않는 레코드
- `fkDomain`: FK concept 값이 지정된 도메인에 속하지 않는 레코드
  (예: gender_concept_id에 Gender 도메인이 아닌 concept)
- `isStandardValidConcept`: concept 컬럼의 값이 표준(standard)·유효(valid) concept이 아닌 레코드
- `plausibleValueLow`: 값이 타당한 하한보다 작은 레코드
- `plausibleValueHigh`: 값이 타당한 상한보다 큰 레코드
  **주의: 하한·상한 값이 요청에 명시된 경우에만 사용하라.**
  기준이 주어지지 않았다면 이 두 지표를 쓰지 말고, 대신 값의 의미상 자명한
  위반(예: 측정값이 음수)만 규칙화하라.

### 일관성 (consistency) — 날짜·시간 논리
- `plausibleStartBeforeEnd`: 종료일이 시작일보다 빠른 레코드
- `plausibleAfterBirth`: 사건 날짜가 출생일보다 이른 레코드
- `plausibleBeforeDeath`: 사건 날짜가 사망일보다 늦은 레코드
- `plausibleDuringLife`: 사망 이후에 발생한 것으로 기록된 레코드
- `plausibleTemporalAfter`: 한 날짜가 기준이 되는 다른 날짜보다 이른 레코드
- `withinVisitDates`: 사건 날짜가 해당 방문 기간을 벗어난 레코드

### 유일성 (uniqueness)
현재 SQL 출력 계약(단일 SELECT, 두 컬럼)으로는 중복 검사를 표현할 수 없어
이 차원의 지표는 제공하지 않는다. 중복 검사 요청이 들어오면 규칙을 만들지 말고
"현재 지원하지 않는 검사 유형"임을 description에 명시한 규칙 0건으로 응답하라.
"""