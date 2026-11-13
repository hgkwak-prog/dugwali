"""슬랙 없이 확인하는 CLI.

  python -m dugwali.cli note "방금 김과장님 통화함. 견적서 금요일까지 다시 달라고 함"
  python -m dugwali.cli transcribe 녹음.m4a      # 전사 속도·화자 구분 측정
  python -m dugwali.cli digest
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
    else:
        rec = pipeline.process(Store(cfg.db_path), args.text, "cli")
        d = rec["data"]
        for label, fn in (("슬랙", render.slack), ("메일", render.mail), ("구두 보고", render.verbal)):
            print(f"\n===== {label} =====\n{fn(d)}")
        if d["ask_user"]:
            print("\n===== 되묻기 =====\n" + "\n".join(d["ask_user"]))
        print(f"\n로그: {cfg.log_dir / (rec['created_at'][:10] + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
