# ModMesh

여러 게임·배포처의 모드를 관리하고 재현 가능한 모드팩으로 공유하는 데스크톱 프로그램입니다.

## 개발 상태

01~02번: Python 패키지, PySide6 데스크톱 기본 창, FastAPI 서버 health 엔드포인트와 빌드·품질 검증 기반을 구현했습니다. 게임 지원과 모드 관리는 후속 단계입니다.

## 개발 환경

Python **3.12**와 가상환경을 사용합니다. PowerShell 기준:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -c requirements.lock ".[desktop,server,dev]"
.venv\Scripts\modmesh
.venv\Scripts\modmesh-server
```

Linux 개발 환경에서는 `.venv/bin/python`, `.venv/bin/modmesh`를 사용합니다. Linux/macOS 제품 지원이나 Windows 독립 실행 패키지는 아직 검증하지 않았습니다.

```powershell
.venv\Scripts\python -m ruff check src
.venv\Scripts\python -m ruff format --check src
.venv\Scripts\python -m mypy
.venv\Scripts\python -m pip wheel --no-deps --no-build-isolation -w dist .
.venv\Scripts\modmesh --check
.venv\Scripts\modmesh-server --check
```

화면 없는 개발 환경은 `QT_QPA_PLATFORM=offscreen`으로 Qt 실행을 확인합니다. `--check`는 실제 Qt 창과 이벤트 루프 / FastAPI health 응답을 실행한 뒤 종료합니다. 서버 기본 주소는 `http://127.0.0.1:8731/health`입니다. 계정·팩 API는 서버 단계에서 구현합니다.

## 구조

- `src/modmesh/apps/desktop`: Qt 실행과 화면
- `src/modmesh/apps/server`: FastAPI 실행
- `src/modmesh/core`: UI와 배포처에 독립적인 공통 로직
- `src/modmesh/adapters`: 게임과 모드 배포처 연동

기준 문서는 [최종 개발계획서](docs/development-plan.md), 진행 상태는 [로드맵](docs/roadmap.md)입니다. 의존성은 `pyproject.toml`과 `requirements.lock`으로 고정합니다.
