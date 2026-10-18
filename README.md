# ModMesh

여러 게임·배포처의 모드를 관리하고 재현 가능한 모드팩으로 공유하는 데스크톱 프로그램입니다.

## 개발 상태

01~24번을 구현했습니다. 공통 코어, Palworld 탐지, 배포처 연동, 통합 검색·검증 다운로드·캐시를 CLI로 사용할 수 있습니다. PySide6는 기본 창, FastAPI는 health 엔드포인트까지입니다. 설치·관리 화면·계정·팩 기능은 후속 단계이며, 실게임/인증 API의 미검증 항목은 [검증 범위](docs/verification.md)에 명시했습니다.

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
.venv\Scripts\python -m ruff check src tools
.venv\Scripts\python -m ruff format --check src tools
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

`[client]`에는 `data_dir`, `cache_dir`, `cache_max_bytes`, `server_url`, `log_level`을 지정할 수 있습니다. `MODMESH_DATA_DIR`, `MODMESH_CACHE_DIR`, `MODMESH_CACHE_MAX_BYTES`, `MODMESH_SERVER_URL`, `MODMESH_LOG_LEVEL` 환경 변수가 파일보다 우선합니다. 상대 경로는 설정 파일 기준입니다. 서버 URL은 HTTPS 또는 로컬 개발용 loopback HTTP만 허용합니다. 공식 서버 없이 로컬 앱을 실행할 수 있습니다.

서버는 `--config`/`MODMESH_SERVER_CONFIG`의 `[server]` 섹션과 `MODMESH_BIND_HOST`, `MODMESH_BIND_PORT`, `MODMESH_ENV`, `MODMESH_DATABASE_URL`을 사용합니다. DB 자격 증명은 환경 변수로 전달하세요. 현재 health API는 DB 없이 실행하며, 실제 계정·팩 DB 연결은 서버 단계에서 구현합니다.

로그는 JSON 형식이며 비밀번호·토큰·인증 헤더·URL 자격 증명과 쿼리 값을 마스킹합니다. 데스크톱 로그는 데이터 경로의 `logs/`에서 크기 제한과 회전을 적용합니다. 서버 오류 응답에는 구조화된 오류 코드와 마스킹된 상세만 포함합니다.

로컬 메타데이터는 SQLAlchemy Core를 통해 SQLite에 저장합니다. 스키마 버전을 확인하고 트랜잭션·WAL·동시 접근 제한을 적용합니다. 알 수 없는 미래 스키마는 덮어쓰지 않습니다. `--check`는 임시 DB를 사용하며 사용자 DB를 변경하지 않습니다.

## 공통 코어와 탐지

게임·모드·파일·의존성 모델, 출처 ID에 근거한 동일성 매핑, SHA-256 파일 식별, 스트리밍 해시 검증, 게임/출처 capability와 Registry를 구현했습니다. 출처 해시가 없으면 무결성을 확인했다고 표시하지 않으며, 파일명이나 모드 이름으로 동일성을 추측하지 않습니다.

`modmesh-cli discover`로 등록된 게임 어댑터의 탐지를 실행합니다. `--game <ID> --path <경로>`는 어댑터 검증을 통과한 수동 경로만 저장합니다. 탐지 엔진은 취소·오류 격리·중복 제거·안정적인 설치 ID를 지원합니다. 현재 Palworld Windows Steam 어댑터를 등록했습니다. Steam 라이브러리·앱 매니페스트와 수동 경로에서 게임 파일 배치를 식별하며, Steam build ID와 게임의 의미상 버전은 구분합니다. 의미상 버전은 확인되지 않으면 미확인으로 남깁니다. **실제 Palworld 설치 환경을 사용할 수 없어 실게임 검증은 미완료입니다.**

`modmesh-cli discover --mods`는 인식된 설치의 모드 파일을 변경 없이 조회합니다. Palworld의 알려진 모드 폴더와 UE4SS 배치를 구분하고 기존 파일은 외부 설치로 표시합니다. 활성 값은 UE4SS 설정을 읽은 결과이며 실제 게임 로딩 성공을 보증하지 않습니다.

Nexus는 공개 trending feed와 공식 다운로드 페이지 인계를 제공합니다. API 기반 상세·파일 조회는 `MODMESH_NEXUS_API_KEY`가 필요합니다. 전체 카탈로그 검색과 동일하지 않으며 계정 권한에 따른 다운로드 제한을 우회하지 않습니다. [검증 범위와 제약](docs/verification.md)을 참고하세요.

Modrinth는 Minecraft Java 모드 검색, 버전·로더 필터, 상세·파일·의존성·체크섬 조회와 허용된 CDN URL 확인을 지원합니다. 실제 API로 검증했으며 파일 저장과 강한 체크섬 검증도 확인했습니다.

CurseForge는 승인된 `MODMESH_CURSEFORGE_API_KEY`로 Minecraft Java 모드 API를 사용합니다. 명시적인 제3자 배포 허용과 API의 다운로드 URL이 확인되어야 직접 다운로드하며, 나머지는 공식 사이트로 인계합니다. 현재 인증 API 실환경 검증은 키 제공 전까지 미완료입니다.

## 통합 검색 CLI

```powershell
.venv\Scripts\modmesh-cli search sodium --game minecraft --loader fabric --game-version 1.21.1 --limit 10
.venv\Scripts\modmesh-cli search "" --game palworld --source nexus
```

각 출처의 결과·미지원/인증 상태·오류·공식 사이트 인계와 다음 페이지 위치를 JSON으로 반환합니다. `--limit`/`--offset`은 출처별 값이며 전체 검색의 전역 순위나 총합으로 가장하지 않습니다. 한 출처가 실패해도 나머지 결과를 유지합니다. 데스크톱 검색 화면은 32번 단계입니다.

통합 검색의 `matching`은 확인된 동일 모드 그룹과 수동 확인이 필요한 후보를 구분합니다. 이름·제작자·저장소가 비슷한 것만으로 확정하지 않습니다. 명시적인 근거로 연결한 매핑과 사용자가 거절한 후보는 로컬에 보존합니다.

기본 출처 순서는 Minecraft의 경우 Modrinth → CurseForge → Nexus, Palworld는 현재 검증된 게임 매핑이 있는 Nexus입니다. `[client.source_priorities]` 테이블의 게임 ID별 배열로 순서를 바꿀 수 있습니다. 이는 출처 활성화나 인증을 대신하지 않습니다. 강한 동일 파일 근거·게임 버전/로더 호환성·다운로드 권한이 확인된 후보만 대체 출처로 사용할 수 있으며, 원래 선택한 버전은 유지합니다.

## 검증 다운로드 CLI

```powershell
.venv\Scripts\modmesh-cli versions --game minecraft --source modrinth --project sodium --game-version 1.21.1 --loader fabric --limit 5
.venv\Scripts\modmesh-cli download --game minecraft --source modrinth --project AANobbMI --version SMxNOGZ6 --game-version 1.21.1 --loader fabric --output ./sodium.jar --progress
```

`versions`가 반환한 정확한 출처 버전/파일 ID를 사용합니다. 파일이 여러 개이고 primary가 유일하지 않으면 `--file`로 선택해야 합니다. 다운로드는 설치를 실행하지 않습니다. 큐·동시 실행 제한·진행률·취소·일시 오류 재시도와 출처 허용 CDN 범위를 적용합니다. 기존 대상 파일은 덮어쓰지 않으며 임시 파일에서 모든 제공 체크섬과 크기를 검증한 뒤 게시합니다. 출처 해시가 없으면 `unverified`, MD5/SHA-1만 있으면 `weak`입니다. API 인증 헤더를 CDN으로 전달하지 않습니다. 직접 다운로드 권한이 없으면 공식 외부 흐름 안내를 반환합니다.

## 검증 캐시

다운로드는 설정한 캐시에 SHA-256으로 저장하고 요청된 출처의 모든 체크섬과 크기를 재검증한 뒤 재사용합니다. 동일한 출처 파일/공통 강한 해시 요청에는 프로세스 간 잠금을 사용합니다. `--output`을 생략하면 캐시 경로를 반환하며, 지정하면 독립된 복사본으로 내보냅니다. 캐시 경로는 정리 시 사라질 수 있으므로 보관할 파일은 내보내세요. 캐시 적중 시에도 CLI의 버전·권한 메타데이터 조회에는 네트워크/배포처 인증이 필요할 수 있습니다.

```powershell
.venv\Scripts\modmesh-cli cache info
.venv\Scripts\modmesh-cli cache verify
.venv\Scripts\modmesh-cli cache prune
.venv\Scripts\modmesh-cli cache clear
.venv\Scripts\python tools/verify_safety.py
```

기본 용량은 콘텐츠 **5 GiB**이며 `[client].cache_max_bytes` 또는 `MODMESH_CACHE_MAX_BYTES`에서 바이트 단위로 바꿉니다. 새 콘텐츠를 넣거나 재사용할 때 제한을 적용하고 오래 사용하지 않은 항목부터 정리합니다. `prune`은 현재 제한까지, `clear`는 추적 중인 콘텐츠 전체를 정리합니다. 파일 손상이 발견되면 재사용하지 않으며 `verify`로 전체 검사할 수 있습니다.

캐시는 지정 경로 아래 `modmesh-cache-v1` 전용 공간을 사용합니다. 소유 표시 없는 비어 있지 않은 공간은 채택하지 않고, DB 기록과 inode 소유 근거가 맞는 콘텐츠만 삭제합니다. 미등록 파일·다른 inode로 교체된 파일·심볼릭 링크는 보존합니다. 갑작스러운 종료로 남은 미등록 임시 파일도 소유를 추측해 삭제하지 않습니다. 용량 수치는 관리하는 콘텐츠에 대한 값이며 이 미등록 파일과 인덱스/잠금 파일은 제외됩니다. API 키나 다운로드 URL은 캐시 인덱스에 저장하지 않습니다. 안전한 게시를 위해 하드 링크와 안정적인 파일 식별을 지원하는 파일시스템이 필요합니다(Windows CI는 NTFS).
