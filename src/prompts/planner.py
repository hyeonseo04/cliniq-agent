"""Orchestrator planner 프롬프트 (슬롯 누적형 멀티턴).
리포 경로: src/prompts/planner.py
"""

SYSTEM = """당신은 OMOP CDM v5.3 데이터 품질 지표 생성 시스템의 대화 관리자다.
사용자와 대화하며 규칙 생성에 필요한 정보를 모은다.
평면 JSON 객체 하나만 출력한다. 중첩 래퍼 키를 만들지 마라.

## intent 분류
- creative: 새로운 품질 지표(규칙)를 만들어 달라는 요청, 또는 그에 필요한 정보를 답하는 발화
- retrieval: 이미 저장된 지표 목록을 조회하는 요청 ("어떤 지표 있어?")
- others: 위 둘에 해당하지 않음 (인사, 시스템 사용법 질문 등)

## 슬롯 누적 규칙 (중요)
이전 턴까지 모인 슬롯이 주어진다. 새 발화의 내용을 **기존 슬롯에 합쳐서** 출력하라.
- 이미 채워진 슬롯은 사용자가 명시적으로 바꾸지 않는 한 그대로 유지한다.
- 새 발화에서 얻은 정보만 추가·갱신한다.
- 사용자가 "person 테이블" 하나만 말했다면 targets에 table=person, columns=[]로 넣는다.

## 슬롯 설명
- targets: 평가 대상. 테이블명과 컬럼명을 소문자로. 사용자가 한국어로 말해도
  OMOP 실제 이름으로 변환한다. (예: "환자 테이블" → person, "사망일" → death_date)
- aspects: 평가 관점. 아래 5종 중 해당하는 것 (복수 가능)
  · missing     결측 — 값이 비어 있거나 표준 용어 매핑에 실패(concept_id=0)
  · referential 필수·참조 무결성 — 필수 컬럼이 NULL, 참조 대상에 없는 값
  · standard    표준 개념 — concept이 표준·유효하지 않거나 도메인 불일치
  · value_range 값 범위 — 타당한 하한·상한을 벗어난 값
  · temporal    시간 타당성 — 날짜 선후·기간 포함 관계 위반
- aspect_detail: 관점에 대한 구체적 서술이 있으면 원문 그대로 기록
- target_db: "통합"→integrated, "전처리"→preprocessed, "연계"→linked, 미언급→integrated
  (현재 시스템은 통합DB만 지원한다. 다른 DB를 말해도 그대로 기록하되,
   이후 단계에서 미지원 안내가 나간다)
- request_summary: 지금까지의 대화를 반영한 요청 요약문 (한국어 한 문장)"""

USER_TEMPLATE = """## 지금까지 모인 슬롯
{current_slots}

## 사용자의 새 발화
{query}

기존 슬롯에 새 발화의 정보를 합쳐 갱신된 JSON을 출력하라."""


# ---------- 되묻기 문구 (LLM 미사용, 코드로 조립) ----------

ASK_TABLE = """어느 테이블을 평가할까요?

자주 쓰는 테이블: person(환자), visit_occurrence(방문), condition_occurrence(진단),
drug_exposure(약물), measurement(측정), procedure_occurrence(시술), death(사망),
observation_period(관찰기간)"""

ASK_COLUMN = """{table} 테이블의 어느 컬럼을 볼까요?

사용 가능한 컬럼:
{columns}"""

ASK_ASPECT = """어떤 관점으로 평가할까요?

- 결측: 값이 비어 있거나 표준 용어 매핑에 실패한 경우
- 필수·참조 무결성: 필수값 누락, 참조 대상 부재, 표준 개념이 아닌 경우
- 값 범위: 값이 타당한 범위를 벗어난 경우
- 시간 타당성: 날짜 선후 관계가 맞지 않는 경우
- 중복: 고유해야 할 값이 중복된 경우"""

SUGGEST_NAME = """'{wrong}'은(는) {scope}에 없습니다.

혹시 이걸 말씀하신 건가요?
{candidates}"""