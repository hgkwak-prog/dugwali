"""두서없는 메모·전사문 → 공통 스키마(dict) 추출."""
from datetime import datetime

from . import llm

KINDS = ["통화", "회의", "지시", "인터럽트", "작업기록", "기타"]
_WEEKDAYS = "월화수목금토일"

PROMPT = """당신은 한국 회사 인턴의 업무 기록 비서입니다. 아래 [입력]은 통화·회의·구두 지시 직후 또는
작업 중에 두서없이 적은 메모이거나 통화 전사문입니다. 이를 보고·기록에 쓸 수 있게 구조화하세요.

오늘: {today} ({weekday}요일), 입력 경로: {source}
{cases_block}
규칙
- 입력에 없는 사실을 지어내지 않는다. 모르면 null 또는 빈 배열.
- 모든 문장은 짧은 개조식 명사형 종결("~함", "~예정", "~필요").
- conclusion: 상사가 이것만 읽어도 되는 결론 한 문장(두괄식). 무엇이 결정됐는지/무엇이 필요한지.
- todos.owner: 작성자 본인이면 "나", 그 외에는 이름·직함. 불명이면 null.
- todos.due: 날짜를 특정할 수 있으면 YYYY-MM-DD("내일", "금요일까지" 등은 오늘 기준으로 환산),
  아니면 원문 표현, 언급 없으면 null.
- resume_point: 하던 일이 끊긴 경우에만 "무엇을 어디까지 했고 다음에 무엇부터"를 한 줄로. 아니면 null.
- ask_user: 보고에 꼭 필요한데 빠진 정보(담당자, 기한, 공유 대상, 상대방)를 묻는 질문. 최대 2개,
  없으면 빈 배열. 사소한 것은 묻지 않는다.
- spoken: 상사에게 말로 보고할 30초 대본. 존댓말 구어체 3~4문장, "결론 → 근거 → 내가 할 일 → 확인 부탁드릴 점" 순서.
- related_case: [진행 중인 건] 목록이 주어졌고 입력이 그중 하나와 같은 일(같은 상대·같은 안건의 후속)이
  확실하면 그 번호, 애매하거나 목록이 없으면 null. 주제가 비슷하다는 이유만으로 고르지 않는다.
- 입력이 "[기록 n]"으로 나뉜 여러 기록이면 한 건의 진행 경과다. 시간순으로 읽고 최신 상태 기준으로 하나로
  정리한다. 뒤 기록에서 끝났거나 바뀐 내용은 앞 기록보다 우선하고, 이미 끝난 일은 todos에서 뺀다.
- 전사문이면 화자 표기(화자1/화자2)를 참고하되, 잡담은 버리고 업무 내용만 남긴다.

아래 형태의 JSON 객체 하나만 출력하세요.
{{
  "kind": "{kinds} 중 하나",
  "title": "15자 내외 제목",
  "conclusion": "결론 한 문장",
  "counterpart": "상대방(이름/소속) 또는 null",
  "decisions": ["결정된 사항"],
  "todos": [{{"task": "할 일", "owner": "나|이름|null", "due": "YYYY-MM-DD|원문|null"}}],
  "open_questions": ["아직 안 정해졌거나 확인이 필요한 것"],
  "background": ["맥락·근거(최대 3개)"],
  "resume_point": "문자열 또는 null",
  "share_to": "공유·보고 대상 또는 null",
  "spoken": "구두 보고 대본",
  "related_case": "번호(정수) 또는 null",
  "ask_user": ["질문"]
}}

[입력]
{text}
"""


def _s(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v if v and v.lower() != "null" else None


def _int(v) -> int | None:
    try:
        return int(str(v).lstrip("#"))
    except (TypeError, ValueError):
        return None


def _list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s for s in (_s(x) for x in v) if s]


def normalize(d: dict) -> dict:
    """LLM 출력이 어떻든 렌더러가 믿고 쓸 수 있는 형태로 맞춘다."""
    todos = []
    for t in d.get("todos") or []:
        if isinstance(t, str):
            t = {"task": t}
        if isinstance(t, dict) and _s(t.get("task")):
            todos.append(
                {"task": _s(t["task"]), "owner": _s(t.get("owner")), "due": _s(t.get("due"))}
            )
    kind = _s(d.get("kind")) or "기타"
    return {
        "kind": kind if kind in KINDS else "기타",
        "title": _s(d.get("title")) or "제목 없음",
        "conclusion": _s(d.get("conclusion")) or "(결론을 뽑지 못함)",
        "counterpart": _s(d.get("counterpart")),
        "decisions": _list(d.get("decisions")),
        "todos": todos,
        "open_questions": _list(d.get("open_questions")),
        "background": _list(d.get("background"))[:3],
        "resume_point": _s(d.get("resume_point")),
        "share_to": _s(d.get("share_to")),
        "spoken": _s(d.get("spoken")),
        "ask_user": _list(d.get("ask_user"))[:2],
        "related_case": _int(d.get("related_case")),
    }


def extract(text: str, source: str = "슬랙 메모", now: datetime | None = None,
            cases: list[tuple[int, str]] | None = None) -> dict:
    """cases: 진행 중인 건 [(번호, 제목)]. 주면 related_case로 관련 건을 제안받는다."""
    now = now or datetime.now()
    cases_block = ""
    if cases:
        cases_block = "\n[진행 중인 건]\n" + "\n".join(f"#{i} {t}" for i, t in cases) + "\n"
    prompt = PROMPT.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=_WEEKDAYS[now.weekday()],
        source=source,
        kinds="|".join(KINDS),
        cases_block=cases_block,
        text=llm.mask(text),
    )
    data = normalize(llm.call_json(prompt))
    if data["related_case"] not in {i for i, _ in cases or []}:
        data["related_case"] = None  # 목록에 없는 번호는 버린다
    return data
