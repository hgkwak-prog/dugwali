"""run.bat이 켤 때마다 부른다: 설정에 맞는 패키지가 설치돼 있는지 보고, 바뀌었으면 설치한다.

- 기본 패키지(requirements.txt)는 항상.
- 이 PC 전사(STT_PROVIDER=local, 기본)일 때만 requirements-stt.txt. 무거워서 API 전사를 쓰는 사람은 받지 않는다.
- Claude API(LLM_PROVIDER=api)일 때만 requirements-api.txt.
- 파일 내용과 고른 목록의 해시를 가상환경 안에 적어 두고, 다를 때만 pip를 돌린다. 업데이트로 의존성이
  늘어도 다시 켜면 설치된다.

표준 라이브러리만 쓴다(패키지를 설치하기 전에 돌아야 하므로). 윈도우 콘솔 인코딩 때문에 출력은 영어로 한다.
실행: python -m dugwali.deps
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP = Path(sys.prefix) / "dugwali-deps.txt"


def read_env(path: Path) -> dict[str, str]:
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip("\"'")
    return out


def wanted(env: dict[str, str]) -> list[str]:
    files = ["requirements.txt"]
    if (env.get("STT_PROVIDER") or "local").lower() == "local":
        files.append("requirements-stt.txt")
    if (env.get("LLM_PROVIDER") or "").lower() == "api":
        files.append("requirements-api.txt")
    return files


def digest(files: list[str], root: Path = ROOT) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f.encode() + b"\0")
        h.update((root / f).read_bytes() if (root / f).exists() else b"")
    return h.hexdigest()


def main(env_path: Path = ROOT / ".env", stamp: Path = STAMP, run=subprocess.call) -> int:
    files = wanted(read_env(env_path))
    want = digest(files)
    try:
        have = stamp.read_text(encoding="utf-8").strip()
    except OSError:
        have = ""
    if have == want:
        return 0
    print(f"[setup] Installing packages ({', '.join(files)}). This can take several minutes...", flush=True)
    args = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    for f in files:
        args += ["-r", str(ROOT / f)]
    code = run(args)
    if code == 0:
        stamp.write_text(want, encoding="utf-8")
    else:
        print("[setup] Package install failed. Check the network and run again.", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
