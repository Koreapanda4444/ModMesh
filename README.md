# ModMesh

Python 3.12, PySide6, FastAPI 기반 데스크톱 모드 매니저입니다.

현재 CLI에서 게임 탐지, 출처별 검색, 버전 조회, 필수 의존성 버전 해결, 해시 검증 다운로드와 캐시를 사용할 수 있습니다. 데스크톱은 기본 창, 서버는 `/health`까지 구현했습니다. 모드 설치 전 변경 계획과 승인 적용·백업·중단 복구를 사용할 수 있습니다. 관리 화면, 프로필·모드팩·계정 기능은 개발 중입니다.

## 설치 및 실행

Windows PowerShell 기준입니다.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -c requirements.lock ".[desktop,server,dev]"
.venv\Scripts\modmesh
.venv\Scripts\modmesh-server
```

서버 기본 주소는 `http://127.0.0.1:8731/health`입니다. Windows 독립 실행 패키지와 Linux/macOS 제품 지원은 아직 검증하지 않았습니다.

## CLI

```powershell
.venv\Scripts\modmesh-cli discover --mods
.venv\Scripts\modmesh-cli search sodium --game minecraft --loader fabric --game-version 1.21.1
.venv\Scripts\modmesh-cli versions --game minecraft --source modrinth --project sodium --game-version 1.21.1 --loader fabric --limit 5
.venv\Scripts\modmesh-cli download --game minecraft --source modrinth --project AANobbMI --version SMxNOGZ6 --game-version 1.21.1 --loader fabric --output ./sodium.jar --progress
.venv\Scripts\modmesh-cli cache info
.venv\Scripts\modmesh-cli cache verify
.venv\Scripts\modmesh-cli cache prune
.venv\Scripts\modmesh-cli cache clear
```

`discover --game palworld --path <경로>`는 검증된 수동 게임 경로를 저장합니다. `download`에는 `versions`가 반환한 정확한 버전 ID를 사용하며, 파일이 모호하면 `--file`로 선택합니다. `--output`을 생략하면 캐시 경로를 반환하고, 지정하면 독립된 복사본으로 내보냅니다. `download` 명령은 파일 준비만 수행합니다.

`resolve`는 `download`와 같은 게임·출처·프로젝트·버전 선택 인자를 받아 필수 의존성을 해결합니다. 정확한 출처 버전 ID와 명시된 SemVer/NPM 범위를 지원하며, 여러 모드의 조건이 맞지 않으면 다른 후보를 탐색합니다. 선택 모드 버전은 고정되고 선택 의존성은 자동 설치하지 않습니다. `solved`와 `metadata_complete`는 각각 제공된 필수 조건의 해결 여부와 의존성 정보의 확인 여부입니다.

의존성 결과에는 누락·버전 불일치·순환·명시적 비호환·게임 버전/로더 충돌 분석도 포함됩니다. 확인되지 않은 의존성·호환성 정보는 경고로 보존하며, 차단 충돌이 있으면 CLI가 실패 상태를 반환합니다.

`plan`은 같은 모드 선택 인자에 `--path <게임 경로>`와 필요한 경우 `--rule <규칙 ID>`, `--archive-prefix <압축 내부 폴더>`를 받아 설치 전 변경 계획과 승인용 fingerprint를 반환합니다. 필요한 파일은 검증 캐시에 준비하며 게임 파일은 변경하지 않습니다. 기존 파일·백업 필요 여부·충돌과 미검증 조건을 표시하고, ZIP 경로 탈출·링크·중복/대소문자 충돌·과도한 압축 해제를 차단합니다. Palworld는 명시적으로 선택한 Pak/Blueprint 또는 알려진 UE4SS 패키지 배치를 구분하며, UE4SS는 로더가 탐지된 경우에만 활성 표식 파일을 계획합니다.

설치하려면 표시된 변경 계획·경고·fingerprint를 검토한 뒤 같은 `plan` 명령에 `--apply <fingerprint>`를 추가합니다. 계획이 바뀌면 새 검토가 필요합니다. 외부 파일 교체는 `--replace-existing`으로 계획에 명시한 경우만 허용하고 원본을 백업합니다. `installed --game <ID> --path <경로>`로 기록을 조회하고 `recover`로 중단 작업의 복구를 시도합니다. 사용자 수정 파일은 보존하며, 복구가 막히면 `recovery_required`와 남은 문제를 표시합니다. 게임을 종료한 상태에서 적용해야 하며 해시 일치가 모드 코드의 안전성을 보장하지는 않습니다.

`remove --game <ID> --path <경로> --source <출처> --project <ID>`로 제거 계획을 검토하고 `--apply <fingerprint>`로 실행합니다. 소유권·해시·파일 식별이 유지되는 파일만 제거하며, 교체 전 백업이 있으면 원본을 복원합니다. 외부 파일·공유 파일·사용자 수정 파일은 보존합니다. 다른 설치 모드가 필수 의존성으로 참조하면 제거를 차단합니다. 의존 모드도 함께 제거하려면 `--also <출처>:<프로젝트 ID>`로 명시적으로 포함해 전체 계획을 검토합니다. 선택하지 않은 불필요 의존성을 자동 제거하지 않습니다. 파일명이 달라지는 업데이트는 먼저 제거 계획을 적용한 뒤 새 설치를 검토해야 합니다.

## 게임 및 출처

| 대상 | 현재 범위와 검증 상태 |
|---|---|
| Palworld Windows Steam | 설치 경로·Steam build ID·Pak/UE4SS 모드 탐지. 실제 게임 설치와 모드 로딩은 미검증. |
| Modrinth | Minecraft Java 모드 검색·버전·로더·의존성·파일 조회. 실제 다운로드와 캐시 재사용 검증. |
| Nexus Mods | 공개 인기 목록과 공식 사이트 인계. 전체 카탈로그 검색에는 제한이 있으며, 인증 상세·다운로드 API는 미검증. |
| CurseForge | 승인 API 키와 배포 권한을 사용하는 Minecraft Java 모드 연동. 승인 계정 API·실다운로드는 미검증. |

Minecraft 게임 인스턴스와 ARK 어댑터는 아직 구현하지 않았습니다. 실제 Modrinth 다운로드 검증은 실행 환경의 OS DNS 제한으로 공개 DoH 응답을 주입해 수행했으며, 제품 기본 경로는 OS DNS입니다.

## 설정

클라이언트는 `--config <TOML>` 또는 `MODMESH_CONFIG`로 설정을 지정합니다. 기본 파일은 `%APPDATA%/ModMesh/config.toml`이며, 없으면 기본값을 사용합니다. `[client]`에는 `data_dir`, `cache_dir`, `cache_max_bytes`, `server_url`, `log_level`과 `source_priorities`를 지정할 수 있습니다. `MODMESH_DATA_DIR`, `MODMESH_CACHE_DIR`, `MODMESH_CACHE_MAX_BYTES`, `MODMESH_SERVER_URL`, `MODMESH_LOG_LEVEL` 환경 변수가 우선합니다.

서버는 `--config` 또는 `MODMESH_SERVER_CONFIG`의 `[server]` 설정과 `MODMESH_BIND_HOST`, `MODMESH_BIND_PORT`, `MODMESH_ENV`, `MODMESH_DATABASE_URL`, `MODMESH_LOG_LEVEL`을 사용합니다. 현재 health API는 DB 없이 동작합니다.

출처 API 키는 `MODMESH_NEXUS_API_KEY`, `MODMESH_CURSEFORGE_API_KEY` 환경 변수로 전달합니다. 인증·배포 제한이 있으면 공식 사이트로 인계하며 직접 다운로드 권한을 우회하지 않습니다.

캐시 기본 용량은 콘텐츠 5 GiB입니다. 재사용 전 해시를 검사하고 용량 초과 시 오래 사용하지 않은 항목부터 정리합니다. 기존 출력 파일과 소유 근거 없는 캐시 파일은 보존합니다. 미등록 파일과 인덱스·잠금 파일은 용량 집계에서 제외됩니다. 출처 해시가 없으면 `unverified`, MD5/SHA-1만 있으면 `weak`로 표시합니다. 파일 게시에는 하드 링크와 안정적인 파일 식별을 지원하는 파일시스템이 필요합니다.

## 개발 검증

```powershell
.venv\Scripts\python -m ruff check src tools
.venv\Scripts\python -m ruff format --check src tools
.venv\Scripts\python -m mypy
.venv\Scripts\python -m pip wheel --no-deps --no-build-isolation -w dist .
.venv\Scripts\modmesh --check
.venv\Scripts\modmesh-server --check
.venv\Scripts\python tools/verify_safety.py
```

화면 없는 환경은 `QT_QPA_PLATFORM=offscreen`을 설정합니다. CI는 Windows·Ubuntu에서 빌드·설치, 기본 실행과 다운로드·캐시 파일 보호를 검증합니다.
