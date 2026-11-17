"""슬랙 없이 확인하는 CLI.

  python -m dugwali.cli note "방금 김과장님 통화함. 견적서 금요일까지 다시 달라고 함"
  python -m dugwali.cli transcribe 녹음.m4a      # 전사 속도·화자 구분 측정
  python -m dugwali.cli digest
  python -m dugwali.cli cases                    # 진행 중인 건 목록
  python -m dugwali.cli case 3                   # 3번 건 통합 정리
  python -m dugwali.cli merge 5 3                # 5번 기록을 3번 건에 합치기
  python -m dugwali.cli delete 5                 # 5번 기록 삭제
  python -m dugwali.cli attention                # 처리 실패, 정리 안 된 기록
  python -m dugwali.cli retry 5                  # 5번 기록을 다시 정리
  python -m dugwali.cli check                    # claude 헤드리스 호출 점검
"""
import argparse
import sys

from . import digest, pipeline
from .agent import llm, render
from .config import cfg
from .store import Store


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dugwali")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("note", help="메모를 정리해 4개 양식으로 출력")
    n.add_argument("text")
    t = sub.add_parser("transcribe", help="오디오 파일 전사")
    t.add_argument("path")
    t.add_argument("--speakers", type=int, default=2)
    sub.add_parser("digest", help="오늘 다이제스트")
    sub.add_parser("check", help="LLM 호출 점검")
    sub.add_parser("cases", help="진행 중인 건 목록")
    c = sub.add_parser("case", help="건 통합 정리")
    c.add_argument("case_id", type=int)
    m = sub.add_parser("merge", help="기록을 다른 건에 합치기")
    m.add_argument("record_id", type=int)
    m.add_argument("case_id", type=int)
    d_ = sub.add_parser("delete", help="기록 삭제")
    d_.add_argument("record_id", type=int)
    sub.add_parser("attention", help="처리 실패와 정리 안 된 기록")
    r_ = sub.add_parser("retry", help="정리에 실패했거나 보관만 한 기록을 다시 정리")
    r_.add_argument("record_id", type=int)
    args = p.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 한글 깨짐 방지
    cfg.ensure_dirs()

    if args.cmd == "check":
        print(llm.call_json('다음 JSON을 그대로 출력: {"ok": true}'))
    elif args.cmd == "transcribe":
        from .stt.transcribe import transcribe

        r = transcribe(args.path, speakers=args.speakers)
        print(r["text"])
        print(f"\n--- 길이 {r['duration']:.0f}초, 처리 {r['elapsed']:.0f}초 "
              f"(실시간 대비 {r['elapsed'] / max(r['duration'], 1):.2f}배), 화자 구분: {r['diarized']}")
    elif args.cmd == "digest":
        print(digest.build(Store(cfg.db_path)))
    elif args.cmd == "cases":
        store = Store(cfg.db_path)
        for c in store.open_cases():
            ids = ", ".join(str(r["id"]) for r in store.case_records(c["id"]))
            print(f"#{c['id']} {c['title']} · 기록 {c['n_records']}건(번호 {ids}) · 할 일 {c['n_todos']}건")
    elif args.cmd == "case":
        data = pipeline.case_summary(Store(cfg.db_path), args.case_id)
        print(render.slack(data) if data else "정리할 기록이 없습니다.")
    elif args.cmd == "merge":
        rec = pipeline.merge(Store(cfg.db_path), args.record_id, args.case_id)
        print(f"기록 {rec['id']} → 건 #{rec['case_id']}")
    elif args.cmd == "delete":
        rec = pipeline.delete(Store(cfg.db_path), args.record_id)
        print(f"기록 {args.record_id} 삭제" if rec else "그런 기록이 없습니다.")
    elif args.cmd == "attention":
        failures, pending = digest.attention(Store(cfg.db_path))
        for f in failures:
            print(f"실패 {f['id']} [{f['stage']}] {f['target']} · 시도 {f['attempts']}회 · {f['error']}")
        for r in pending:
            print(f"정리 안 된 기록 {r['id']} · {r['created_at'][:16]} · {r['source']}")
        if not failures and not pending:
            print("막힌 건이 없습니다. (처리 실패 0건 · 정리 안 된 기록 0건)")
    else:
        store = Store(cfg.db_path)
        try:
            if args.cmd == "retry":
                if not store.get_record(args.record_id):
                    print("그런 기록이 없습니다.")
                    return 1
                rec = pipeline.summarize(store, args.record_id)
            else:
                rec = pipeline.process(store, args.text, "cli")
        except pipeline.ProcessError as e:
            print(f"정리 실패: {e}\n원문은 기록 {e.record['id']}로 저장했습니다. "
                  f"다시 시도: python -m dugwali.cli retry {e.record['id']}")
            return 1
        d = rec["data"]
        for label, fn in (("슬랙", render.slack), ("메일", render.mail), ("구두 보고", render.verbal)):
            print(f"\n===== {label} =====\n{fn(d)}")
        if d["ask_user"]:
            print("\n===== 되묻기 =====\n" + "\n".join(d["ask_user"]))
        print(f"\n기록 {rec['id']} / 건 #{rec['case_id']}"
              + (f" / 관련 건 제안: #{d['related_case']}" if d.get("related_case") else ""))
        print(f"\n로그: {cfg.log_dir / (rec['created_at'][:10] + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
