"""보여줄 때 쓰는 정리 결과. 할 일은 LLM이 뽑았던 목록이 아니라 지금의 할 일 테이블을 따른다.

사람이 할 일을 고치거나 끝내면 슬랙 초안·메일 양식·업무 로그·대시보드가 모두 같은 내용을 보여야 한다.
"""
EDITABLE = ["title", "conclusion", "counterpart", "share_to", "decisions", "open_questions", "background"]
LIST_FIELDS = {"decisions", "open_questions", "background"}
FIELD_LABEL = {"title": "제목", "conclusion": "결론", "counterpart": "상대", "share_to": "공유 대상",
               "decisions": "결정", "open_questions": "확인 필요", "background": "배경"}


def _open_todos(store, cid: int) -> list[dict]:
    return [{"task": t["task"] + (" (완료 추정)" if t["suspect"] else ""), "owner": t["owner"], "due": t["due"]}
            for t in store.case_todos(cid) if not t["done"]]


def case_data(store, cid: int, data: dict) -> dict:
    """건 단위 정리(통합 정리, 또는 기록이 하나뿐인 건의 그 기록)에 지금의 할 일을 입힌다."""
    return {**data, "todos": _open_todos(store, cid)}


def record_data(store, rec: dict) -> dict | None:
    """기록 하나의 정리 결과. 건에 정리된 기록이 이것 하나뿐이면 지금의 할 일을 입힌다.

    여러 기록이 묶인 건에서는 기록마다의 할 일은 그때의 기록으로 두고, 지금의 할 일은 건 단위에서 본다.
    """
    if not rec.get("data"):
        return None
    done = [r for r in store.case_records(rec["case_id"]) if r["data"]]
    if len(done) == 1 and done[0]["id"] == rec["id"]:
        return case_data(store, rec["case_id"], rec["data"])
    return rec["data"]
