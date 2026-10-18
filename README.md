# ModMesh

여러 게임·배포처의 모드를 관리하고 재현 가능한 모드팩으로 공유하는 데스크톱 프로그램입니다.

## 개발 상태

01~03번: Python 패키지, PySide6 데스크톱 기본 창, FastAPI 서버 health 엔드포인트와 빌드·품질 검증 기반을 구현했습니다. 게임 지원과 모드 관리는 후속 단계입니다.

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

## 설정

`modmesh --config <TOML 경로>` 또는 `MODMESH_CONFIG`로 클라이언트 설정을 지정합니다. 기본 파일은 Windows의 `%APPDATA%/ModMesh/config.toml`입니다. 파일이 없으면 로컬 기본값을 사용합니다.

`[client]`에는 `data_dir`, `cache_dir`, `server_url`, `log_level`을 지정할 수 있습니다. `MODMESH_DATA_DIR`, `MODMESH_CACHE_DIR`, `MODMESH_SERVER_URL`, `MODMESH_LOG_LEVEL` 환경 변수가 파일보다 우선합니다. 상대 경로는 설정 파일 기준입니다. 서버 URL은 HTTPS 또는 로컬 개발용 loopback HTTP만 허용합니다. 공식 서버 없이 로컬 앱을 실행할 수 있습니다.

서버는 `--config`/`MODMESH_SERVER_CONFIG`의 `[server]` 섹션과 `MODMESH_BIND_HOST`, `MODMESH_BIND_PORT`, `MODMESH_ENV`, `MODMESH_DATABASE_URL`을 사용합니다. DB 자격 증명은 환경 변수로 전달하세요. 현재 health API는 DB 없이 실행하며, 실제 계정·팩 DB 연결은 서버 단계에서 구현합니다.
