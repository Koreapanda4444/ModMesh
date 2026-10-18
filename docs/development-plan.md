# ModMesh — 최종 개발계획서 및 Work 인계 문서

> 기준일: 2026-10-18  
> GitHub: https://github.com/Koreapanda4444/ModMesh  
> 대상: 실제로 완성·배포·사용할 수 있는 범용 **데스크톱 모드 매니저**  
> 계획: **53개 예상 커밋** (기존 52개 + 계정 화면 1개). 숫자보다 검증 가능한 기능과 최종 완성도를 우선한다.  
> 상태: **계획서만 작성한 상태**. 이 파일을 만든 행위로 GitHub 코드를 변경하지 않았다. Work는 시작 시 최신 저장소를 재확인할 것.

## 1. 프로젝트 목표와 제품 경계

ModMesh는 여러 게임의 모드를 한 프로그램에서 탐색·설치·삭제·관리하고, 구성한 모드 환경을 프로필 및 **재현 가능한 모드팩**으로 저장하며, 계정을 통해 모드팩을 게시하고 공유 코드로 전달할 수 있는 범용 모드 관리 프로그램이다.

목표는 임시 MVP가 아닌 **기획 범위를 끝까지 구현한 사용 가능한 제품**이다. 내부 우선순위가 있다고 해서 나중 단계 기능을 임의로 취소하지 않는다. 실제 플랫폼 정책 때문에 제한되는 작업은 그 사실을 표시하며 허용된 대체 흐름을 제공한다.

제품 원칙:

1. **Game Adapter / Source Adapter / Core 독립성:** 게임과 배포처가 늘어나도 엔진을 통째로 수정하지 않는다.
2. **기존 파일 우선 보호:** 모드 설치·제거 전에 변경 계획을 작성하고, 본래 파일과 앱이 소유·수정한 파일을 추적한다.
3. **로컬 완전 동작:** 회원가입이나 자체 서버가 없어도 검색·다운로드·설치·제거·프로필·로컬 모드팩 기능을 사용할 수 있다(외부 배포처의 인증 요구는 별개).
4. **불확실성 표시:** 동일 모드 식별, 해시, 다운로드 가능성, 버전 호환성, 재현 가능성에 근거가 없으면 추측해서 처리하지 않는다.
5. **실제 동작 검증:** UI 껍데기나 더미 응답만 구현하고 완료라고 하지 않는다.

### 반드시 구현할 범위

- 자동 게임 탐지 및 수동 경로 지정; 게임 버전·모드 환경·지원 조건 확인.
- 여러 출처 통합 검색, 모드 상세 정보, 버전·로더·게임별 필터, 동일 모드/동일 파일 식별.
- 권한이 허용되는 다운로드, 중단·취소·재시도, 캐시, 해시 검증, 설치 진행률.
- 의존성·충돌 탐지, 설치 계획과 확인, 안전한 파일 적용·제거·복구, 설치 기록.
- 게임별 프로필(생성/전환/관리), 설정 파일 관리(허용 범위 내).
- Manifest 기반 모드팩, 로컬 Import/Export, 버전 비교 및 변경 적용.
- 자체 계정(회원가입·로그인·로그아웃), 온라인 팩 게시/버전 관리, 공유 코드 Import, 원격 버전 확인 및 동기화.
- 별도 설치·운영 가능한 Self-hostable ModMesh Server, 사용자 지정 서버 URL.
- 사용자 지정 모드 파일/출처 및 새 게임/출처를 위한 확장 체계.

### 최초 완성 범위에서 제외

게임 실행 런처, 자동 게임 실행, 게임 서버 호스팅, LAN/VPN/LanLink, 실시간 친구 상태/채팅, 세이브 파일 동기화, 모든 게임 모드를 임의로 실행·패치하는 기능, 모드 파일 대규모 재배포 저장소, 강제 업데이트, 결제/구독.

## 2. 게임, 모드 플랫폼, 운영체제

- **첫 운영체제:** Windows x64. 실제 설치 패키지와 실행을 검증할 것. Linux/macOS는 경계를 열어두되 실제 검증·릴리스하지 않은 플랫폼을 지원한다고 표시하지 않는다.
- **Palworld:** Windows PC **Steam** 버전 우선. 설치된 게임과 버전을 탐지하고 실제 모드별 설치 규칙(예: 게임이 지원하는 파일 배치 방식 및 필요한 모드 로더)을 구분한다. 한 가지 폴더 복사 규칙으로 모든 모드가 된다고 가정하지 않는다.
- **Minecraft:** **Java Edition**. Fabric/Forge/NeoForge 등의 로더와 게임 버전을 명시적으로 다루며 기존 다른 런처·사용자 파일을 보호한다. 프로필 전환 시 실제 사용 가능한 모드 환경이 구성되는지 검증한다.
- **ARK:** **ARK: Survival Evolved, Steam PC판 확정**. **ARK: Survival Ascended는 제외**한다. Evolved의 핵심 배포 경로인 **Steam Workshop** 구독·설치 상태 탐지, 게임 설정/모드 ID 연관 및 필요한 경우 Steam 공식 구독/설치 흐름으로 이어지는 기능을 포함한다. 정책·기술 검증 없이 제3자 Workshop 파일 직접 다운로드를 보장하지 않는다.
- **외부 Source:** Nexus Mods, Modrinth, CurseForge, Custom. 유효한 게임/출처 조합에만 검색/다운로드를 제공한다. Steam Workshop은 ARK 지원을 위해 별도 통합/인계 경계로 처리하되, 향후 범용 Source Adapter로 확장할 수 있게 인터페이스를 정한다.

## 3. 최종 기술 스택 선정

**이번 인계서의 구현 기본안은 Python 스택으로 확정한다.** 이는 별도로 작성된 Rust 초안에서 의도적으로 변경한 것이다. 모드 매니저 작업은 주로 HTTP/API, 파일 I/O, 압축, 해시와 DB 중심이며 Rust가 반드시 필요한 순수 CPU 연산이나 메모리 관리 요구는 현재 확인되지 않았다. 최종 실행 성능은 실제 측정으로 검증하고, 병목이 발생할 때 그 부분만 최적화한다.

- **언어:** Python 3.12/3.13 중 의존성 호환성이 확인된 하나로 핀 고정. 타입 힌트 적극 사용.
- **데스크톱 UI:** **PySide6 / Qt 6**, 기본 Qt Widgets 기반의 깔끔한 다크 UI(모델/뷰 컴포넌트, 비동기 작업 중 UI 응답성 유지). React, Tauri, Rust는 기본 의존성이 아니다.
- **로컬 Core:** UI에 종속되지 않는 독립 Python 패키지(도메인, 게임/출처 어댑터, 검색, 의존성, 설치, 캐시, 팩).
- **HTTP:** `httpx` 기반 비동기 요청 및 스트리밍 다운로드, 서비스별 timeout/retry/backoff/요청 제한/`User-Agent`.
- **Local DB:** SQLite + SQLAlchemy 2.x(또는 필요성이 명확할 때 더 단순한 DAO). 버전 있는 마이그레이션.
- **Server:** **FastAPI** + PostgreSQL + SQLAlchemy 2.x + Alembic; 사용자, 인증, 팩, 팩 버전, 공유 코드 API.
- **인증:** 검증된 패스워드 해시(Argon2id), 서버측 세션/토큰, OS 보안 저장소를 통한 데스크톱 인증정보 저장(사용 가능 플랫폼에서). 비밀 정보를 평문 설정이나 로그에 남기지 않는다.
- **배포:** Windows 독립 실행 파일/설치 패키지(PyInstaller 등 적합 도구로 실제 검증), 서버 Docker/Standalone; 특정 클라우드 전용 API 미사용.
- **품질:** `ruff`, 타입 검사(`mypy` 또는 `pyright` 중 선택), 실행 검증, 최소한의 안전성 회귀 테스트. 라이브러리 버전은 실제 개발 시 검증 후 핀/lock.

**기술 스택 주의:** PySide6/Qt는 LGPL/GPL/상용 라이선스 등 조건이 있으므로 실제 외부 배포 형태(재배포, 동적 링크, 통지·소스 제공 의무 등)를 확인해야 한다. 사용자가 별도로 요청하지 않으면 저장소에 임의의 프로젝트 `LICENSE` 파일은 생성하지 않되, 의존성 라이선스 준수 의무는 생략하지 않는다. PyInstaller의 패키지 크기나 부팅 속도는 실제 빌드로 확인한다.

### 실행 구조

```text
ModMesh Desktop (PySide6 / Qt Widgets)
  ├─ 게임 컨텍스트 선택 · 검색 · 설치된 모드 · 프로필 · 모드팩 · 계정 UI
  ├─ Presentation/ViewModel · background workers · progress signals
  └─ ModMesh Core (독립 Python 패키지)
       ├─ Game Adapter registry: Palworld / Minecraft / ARK Evolved
       ├─ Source Adapter registry: Nexus / Modrinth / CurseForge / Custom
       ├─ Workshop integration boundary (ARK Evolved)
       ├─ Search / identity / artifact matching
       ├─ Dependency resolver / conflict analyzer
       ├─ Installation planner / installer / removal / recovery
       ├─ Download manager / SHA-256 verified cache
       ├─ Profiles / pack manifests / diff
       ├─ SQLite + operation journal
       └─ HTTP client to selected ModMesh Server
                   │ HTTPS (public) / localhost (development)
                   ▼
ModMesh Server (FastAPI)
  ├─ Accounts / verification / login / sessions / ownership
  ├─ Pack / immutable PackVersion / ShareCode / permissions
  └─ PostgreSQL

Mod binaries: 원칙적으로 허용된 원래 플랫폼에서 클라이언트로 내려받음.
```

Core는 Qt 클래스를 참조하지 않는다. UI의 메인 이벤트 루프에서 느린 다운로드·압축·해시·설치 작업을 실행하지 않는다. 긴 작업의 취소·진행률·오류는 worker 이벤트로 전달한다.

### 권장 저장소(빈 폴더만 미리 만들지 않음)

```text
ModMesh/
  apps/
    desktop/        # PySide6 application, windows, widgets, view-models
    server/         # FastAPI app and server configuration
  modmesh/
    domain/         # Models and capability definitions
    core/           # Search/resolver/install/download/cache/profiles/packs
    adapters/
      games/        # palworld, minecraft, ark_evolved
      sources/      # nexus, modrinth, curseforge, custom
      workshop/     # ARK/Steam Workshop boundary when implemented
    storage/        # SQLite repository, migrations, journals
    remote/         # API client, auth session isolation
  README.md
  pyproject.toml
```

구조는 개념적인 기준이다. 실제 패키징과 배포 적합성을 검증하면서 중복 폴더를 줄여도 된다.

## 4. 데스크톱 화면/UX 설계

**기본 프레임:** 상단 게임·활성 프로필 선택, 좌측 사이드바, 중앙 콘텐츠, 우측 선택 항목 상세 패널. 모달은 설치 확정·위험한 삭제 등 명시적 확인이 필요한 때만 사용한다. 다크 테마, 키보드 탐색, 읽기 쉬운 정보 밀도, 상태/오류 안내.

1. **내 게임:** 설치 감지 카드/목록, 지원 게임과 미탐지 게임 구분, 실제 경로·버전·검증 상태, 재탐색, 수동 경로 지정.
2. **모드 탐색:** 선택 게임 기준 통합 검색, 버전·로더·출처·정렬 필터, 설치 가능 여부/제약. 우측 패널에 설명, 제작자, 버전, 출처, 의존성, 변경 계획 진입.
3. **설치된 모드:** 현재 프로필 내 설치 목록, 관리 중인 파일과 외부 설치 파일 구분, 업데이트·호환성 표시, 안전 제거.
4. **프로필:** 생성·복제·이름 수정·전환·삭제, 활성 프로필, 포함 모드/설정 차이 표시. 전환 전에 실제 파일 영향 미리보기.
5. **모드팩:** 프로필을 팩으로 저장, Manifest 미리보기, Import/Export, 버전 diff, 팩 게시/편집, 공유 코드 입력.
6. **다운로드/작업:** 요청 큐, 진행률, 단계, 취소·재시도, 해시 실패, 설치/복구 상태와 확인 가능한 작업 로그.
7. **계정:** 회원가입·이메일 확인·로그인·로그아웃, 세션 상태, 비밀번호 재설정, 본인 게시 팩. 게스트 모드 유지.
8. **설정:** 경로, 캐시 크기/정리, 배포처 연결(각 서비스 권한 안내), 서버 URL(공식/사용자 서버), 진단 로그/버전/정보.

첫 화면은 ‘내 게임’으로 두되, 실제 사용자 편의상 마지막 게임·프로필 복원. 상세 패널과 필터는 각 게임 컨텍스트로 유지. 화면은 목업에 그치지 않고 Core API에 연결한다.

## 5. 어댑터·데이터 모델과 통합 검색

### Game Adapter

설치 여부/경로, 게임 버전 및 판본, 지원 플랫폼, 모드 경로, Loader/Workshop 상태, 설치된 모드 탐지, 설치/제거 규칙, 환경 검증, 실행 중 잠금·구성 충돌을 처리한다. 게임별 코드를 공통 installer 내부에 하드코딩하지 않는다.

### Source Adapter

검색, 프로젝트 상세, 버전·파일·의존성, 지원 게임/로더, 해시, 다운로드 가능 여부, 직접 다운로드/웹 인계 방식을 표현한다. 플랫폼 정책과 API 권한에 따른 **capability**(예: `search`, `versions`, `download_direct`, `external_handoff`, `dependencies`)를 명시한다.

### 모델과 동일성

핵심 모델: `Game`, `GameInstallation`, `GameVersion`, `Mod`, `ModVersion`, `SourceProject`, `ModFile`, `Artifact`, `Dependency`, `InstalledMod`, `Profile`, `Operation`, `Pack`, `PackVersion`, `User`, `ShareCode`.

- 비슷한 모드 이름 == 동일 모드라는 판단 금지. 검증 가능한 ID 매핑과 파일 해시 기반의 강한 식별 정보를 기록한다.
- 동일한 버전명이어도 해시가 다르면 다른 Artifact다. 해시가 일치하고 맥락이 맞는 경우에만 동일 파일로 묶는다.
- 여러 출처 검색 결과는 같은 canonical mod의 후보만 중복 표시를 줄이고, 불확실한 매칭은 수동 분리 가능해야 한다.
- 출처 우선순위는 게임별로 다르게 구성하고, 정책·권한·호환성·파일 동일성이 검증되었을 때만 fallback한다.
- 일부 플랫폼이 해당 게임을 지원하지 않거나 토큰이 없는 경우 그 출처만 실패 처리하고 전체 검색을 실패시키지 않는다.

## 6. 실제 설치 엔진의 핵심 요구

### 계획 → 다운로드 → 검증 → 적용 → 기록

1. 선택한 게임 설치 경로·게임 버전·로더·활성 프로필·모드 파일을 식별.
2. `required`, `optional`, `incompatible`, `embedded` 의존성 수집; 버전 제약·순환·상호 배타·누락을 해결하거나 명확히 실패.
3. 변경 예정 파일, 백업 파일, 예상 충돌, 공유 의존성, 경로, 설치 가능 여부를 **사전 계획**으로 표시.
4. 허용되는 파일만 임시 위치로 다운로드. 취소/재시도와 체크섬/해시 검증. 미확인 해시는 '검증 없음'으로 표기한다.
5. 압축 검사: Zip Slip/절대 경로/링크/비정상적으로 큰 압축 해제/중복 파일명(Windows 대소문자 포함)/잘못된 대상 경로 등 차단.
6. 준비한 파일을 가능한 한 안전한 교체 방식으로 적용하고 파일 소유권·해시·원본/백업·작업 저널을 기록.
7. 중단·오류 발생 시 복구를 시도하고 실패하면 사용자에게 정확한 상태·복구 가능 범위를 표시. '무조건 원자적'이라고 주장하지 않는다.
8. 삭제 시 앱 소유 파일 여부, 사용자 수정 여부, 다른 모드의 공유 의존성 여부를 확인. 타인이 소유한 파일·사용자가 수정한 파일을 임의 삭제하지 않는다.

모드 소프트웨어 자체는 신뢰할 수 없는 제3자 코드일 수 있다. 해시 일치는 전송 무결성일 뿐 안전성 보장이 아님을 안내한다. 알 수 없는 설치 스크립트·실행 파일 자동 실행을 기본 동작으로 만들지 않는다.

### 캐시·작업 상태

SHA-256 기반 캐시, 재다운로드 방지, 용량 제한, 안전한 캐시 정리. 영속적인 작업 저널로 갑작스러운 종료 뒤 불완전 상태 발견·복구. 실제 프로필 전환 시 게임별 저장 공간과 설치 규칙을 반드시 확인한다.

## 7. 프로필과 모드팩

- **프로필:** 로컬 게임별 독립적인 모드 구성이며 버전/로더/설정을 포함한다. 격리 폴더 사용이나 파일 교체 전략은 Game Adapter마다 검증한다. 기존 사용자 파일을 덮지 않는 전환 정책이 필수.
- **모드팩:** 기본적으로 바이너리를 싣지 않는 Versioned Manifest. `schema_version`, `game_id`, `edition`, `platform`, `game_version`, `loader`, `mod entries (source IDs, file IDs, versions, hashes, dependencies)`, 설정 메타데이터 포함.
- Manifest에는 개인 절대 경로·API 키·토큰·시크릿·민감 설정 파일을 기본 포함하지 않는다. 설정 파일을 넣는다면 사용자 확인 및 비밀 값 제거 절차.
- Import는 스키마/게임/버전/파일 존재·권한·해시 확인 → 설치 계획 미리보기 → 사용자 승인 후 적용.
- 버전 스냅샷은 변경 불가능하게 보존하고 vN→vN+1에 추가/삭제/업데이트/설정 변경 diff 제공.
- 저작권/서비스 정책 때문에 원 파일을 내려받을 수 없으면 완벽하게 재현할 수 없다고 알려야 하며 우회·무단 미러링은 하지 않는다.

## 8. 회원가입 및 온라인 서버

### 계정 기능

- ModMesh **자체 계정**을 이메일·비밀번호로 생성; 로그인/로그아웃, 이메일 확인, 비밀번호 재설정·변경, 세션 만료/폐기, 계정 정보 관리. 공개 서비스에는 비밀번호 복구를 포함한 최소한의 계정 복구 체계 필수.
- 비밀번호는 Argon2id 같은 강한 패스워드 해싱. 클라이언트 인증 토큰은 가능하면 OS 보안 저장소에 저장; 로그·Manifest·프로젝트 설정에 노출 금지.
- 인증 요청 rate limiting, 가입·로그인 오용 방지, 소유권과 팩 ACL 서버 재검증, 서비스 URL별 세션 격리.
- 계정 없이 앱 시작 가능. 계정은 온라인 게시/소유 팩 관리에만 필요; 공유 코드 읽기 허용 범위는 서버 공개 설정을 따른다.
- Nexus/CurseForge/Steam 계정은 **ModMesh 자체 계정과 별개**. 타 서비스 자격 증명을 ModMesh 자체 로그인으로 대신할 수 없다.

### ModMesh Server

- 표준 HTTP(S) API + PostgreSQL: `User`, `Session`, `Pack`, `PackVersion`, `Manifest`, `ShareCode`, `visibility`, `ownership`, timestamps.
- 팩: 소유자만 게시·새 버전 생성·폐기/수정 가능. 공개/비공개/코드 소지자 접근(unlisted)을 명확히 구분.
- 공유 코드는 충돌/추측 저항을 갖는 충분한 랜덤 값, 만료·회수/재발급, 횟수 제한. **공유 코드는 팩 수정 권한이 아님**.
- 현재 사용 가능한 소스에서 원본 파일을 받도록 Manifest만 호스팅. 불법 재배포/재호스팅·사용자별 토큰 중계는 피한다.
- 사용자는 공식 서버 또는 커스텀 self-hosted 서버 URL 선택 가능. 공식 서버가 없어도 로컬 기능 작동. 오라클 클라우드 강제하지 않음.
- 서버의 실제 공개 배포 시 도메인/HTTPS, 메일 발송(SMTP/트랜잭션 이메일), DB 백업·복구, 비밀 관리, API throttling, 관리자 기능, 모니터링 및 개인정보 정책 검토가 필요. **개발은 로컬로 진행 가능**.

## 9. 외부 플랫폼 실제 제약

- **Modrinth**: 공식 API 및 `User-Agent` 요구, rate-limit 헤더를 준수. 프로젝트/버전/의존성/호환성 모델 연동.
- **Nexus Mods**: 공개 앱은 Nexus API 등록과 정책 확인이 요구될 수 있다. 개인 API 키를 공개 배포 앱에 임의로 내장하지 않는다. 다운로드 권한/계정 제한이 있으면 공식 흐름으로 넘긴다.
- **CurseForge**: 제3자 API 키 신청/승인과 CDN 다운로드 규정을 확인. 공개 클라이언트에 비밀 API 키를 하드코딩하지 않는다. 승인 없는 직접 다운로드를 보장하지 않는다.
- **Steam Workshop / ARK Evolved**: Steam을 통한 구독/다운로드가 기본 경로. 실사용 테스트 없이 Steam 파일 직접 다운로드나 API 우회를 설계하지 않는다. ID/구독/설치 상태 조회 및 공식 구독 화면 이동을 우선 허용.
- **Custom**: 본인이 이용 권한을 가진 로컬 파일 또는 URL. 안전한 파일 경로·다운로드 URL 검증, 해시·설치 규칙 필수. 알 수 없는 원격 실행·SSRF성 요청 차단.

가능한 기능과 불가능한 기능을 UI에서 명확히 구별한다. 타사 정책이 변할 수 있으므로 adapter 구현 시 공식 문서를 다시 검토한다.

## 10. 릴리스 완료의 판정 기준

- Windows x64에서 독립 실행 가능한 앱으로 설치·실행·재실행 가능.
- Palworld/ Minecraft Java/ ARK Survival Evolved에서 **실제 접근 가능한 테스트 환경**으로 지원 범위 내 모드 검색/설치 상태 탐지 및 안전한 관리 동작 검증. 외부 계정·정책으로 불가능한 부분은 명확히 범위 표시.
- 설치 중 네트워크 실패·해시 불일치·압축 오류·프로그램 종료·사용자 수정 파일·공유 의존성 충돌 시 안정적으로 실패/복구.
- 프로필 전환, 모드팩 Import/Export와 버전 diff가 실제 파일 상태를 바탕으로 작동.
- 회원가입·메일 확인·로그인·로그아웃·세션 만료, 팩 소유권, 공유 코드 생성/회수, 다른 사용자 접근 제한이 서버에서 검증.
- 자가 호스팅 서버를 표준 Linux 환경/Docker에서 배포 가능. 클라이언트의 서버 URL 변경 가능.
- 실제 API가 차단·미승인된 기능은 그 상태를 알려주고, 동작이 검증되지 않았는데 완료라고 하지 않는다.
- 작업 시 lint·타입 검사·빌드·사용 가능한 통합/회귀 검증을 실행. 사용자의 원칙에 따라 **불필요한 `tests/`, fixture, 샘플, LICENSE 파일을 기본적으로 생성하지 않는다**. 다만 실제 파일 손상 가능성이 있는 안전성 검증은 필요 최소한의 자동화로 남겨도 된다.
- GitHub Actions는 실제로 실행 가능한 환경과 명령만 설정한다. 워크플로가 깨진 채로 커밋 진도를 나가지 않는다.

## 11. 개발과 커밋 원칙

- 작업 시작 직전에 저장소와 기존 커밋을 확인. 중복 커밋, 무단 덮어쓰기, 이미 있는 기능 재작성 금지.
- **각 커밋 = 의미 있는 구현 단위**. 해당 기능 구현→검증→오류 수정→커밋→푸시한 뒤 다음으로 이동.
- 53은 계획 번호. 기능이 반드시 분리되어야 하면 늘리고 자연스럽게 합쳐야 하면 합친다. 숫자를 맞추기 위해 코드를 미리 구현하고 나중에 커밋만 쪼개지 않는다.
- 실제 환경이 부족해 검증할 수 없으면 '미검증'이라고 표시하고 필요한 조건만 명확히 요청한다.
- README와 진행 보고에는 실제 구현/동작한 기능만 완료 처리. 쓸모없는 예제 파일, 다량의 더미 데이터, 임의 LICENSE 생성 금지.
- 계정/메일/외부 API/클라우드의 비밀 키는 파일이나 커밋에 넣지 말고 환경 변수 또는 OS 안전 저장소 사용.
- 코드를 무작정 고집하지 않는다. 더 정확한 라이브러리 또는 핵심 설계가 확인되면 근거를 명확히 남기고 바꿀 수 있다.

## 12. 최종 예정 커밋 로그 — 53개

커밋 제목은 Conventional Commits를 기준으로 제안한다. 실제 SHA는 구현·푸시 후 기록한다. 현재 목록은 **실제 커밋 이력이 아니라 예정 커밋 로드맵**이다.

### Foundation
01. `chore: initialize ModMesh project` — 저장소 초기화, README 및 최소 구조.
02. `build: configure desktop and server toolchains` — Python 패키지/가상환경, PySide6/FastAPI 개발도구, 기초 실행/빌드/검증.
03. `feat: add application configuration` — 앱·서버 설정, 환경별 구성, 검증.
04. `feat: add logging and error handling` — 공통 구조화 오류/로그, 보안 마스킹.
05. `feat: initialize local metadata storage` — SQLite 스키마·마이그레이션 및 저장 계층.

### Core Models
06. `feat: define game and mod domain models` — 게임·모드·설치/버전/파일 모델.
07. `feat: add canonical mod identity` — 출처 간 동일 모드 식별 근거.
08. `feat: add file hash and artifact identity` — SHA-256/Artifact 식별·무결성.
09. `feat: define dependency relationships` — required/optional/incompatible/embedded 관계.

### Adapter Architecture
10. `feat: add game adapter architecture` — 독립 Game Adapter 계약 및 Registry.
11. `feat: add source adapter architecture` — Source Adapter/지원 capability/Registry.
12. `feat: implement game discovery engine` — 게임 경로 탐지 및 수동 지정.

### Palworld
13. `feat: add Palworld game support` — Steam 설치·버전·모드 환경 탐지.
14. `feat: add Palworld mod discovery` — 실제 모드 위치/형태/기존 파일 안전 탐지.

### Source Infrastructure
15. `feat: add shared source network layer` — HTTP/재시도/타임아웃/제한/자격 증명 경계.
16. `feat: add Nexus Mods integration` — 정책 준수 검색·정보·허용 다운로드/인계.
17. `feat: add Modrinth integration` — 검색·버전·로더·의존성·파일.
18. `feat: add CurseForge integration` — 허용되는 API 키/검색·조회·다운로드 인계.

### Unified Search
19. `feat: implement unified mod search` — 출처 통합 검색/필터/오류 격리.
20. `feat: add cross-source mod matching` — 동일 모드 후보 매핑.
21. `feat: add identical artifact merging` — 확인된 동일 해시 파일 병합.
22. `feat: add per-game source priorities` — 게임별 출처 우선순위와 fallback.

### Download / Cache
23. `feat: add mod download manager` — 취소·재시도·진행률·다운로드 큐.
24. `feat: add hash verified mod cache` — 검증 캐시 재사용 및 정리.

### Dependencies
25. `feat: add dependency resolver` — 버전 제약/필수 의존성 해결.
26. `feat: add dependency conflict detection` — 충돌·누락·순환·로더 호환성.

### Installer
27. `feat: add mod installation planner` — 설치 전 파일 변경 계획.
28. `feat: implement safe mod installation` — 설치·파일 기록·백업·복구.
29. `feat: implement safe mod removal` — 추적한 모드 파일만 안전 제거.
30. `feat: protect shared dependencies` — 참조 중인 의존성 제거 차단.

### Desktop UI
31. `feat: add game library interface` — 게임 라이브러리/선택/경로 UI.
32. `feat: add integrated mod search interface` — 통합 검색 결과/필터/출처 UI.
33. `feat: add mod details and install actions` — 상세/버전/설치 계획/실행 연결.
34. `feat: add installed mod management` — 설치 목록/업데이트/안전 삭제 UI.
35. `feat: add download and operation progress` — 다운로드/설치 진행·오류·복구 UI.

### Minecraft
36. `feat: add Minecraft game adapter` — Java Edition 인스턴스 탐지.
37. `feat: add Minecraft loader support` — Fabric/Forge/NeoForge별 규칙.
38. `feat: integrate Minecraft multi-source mods` — 호환성 기반 다중 Source 검색/설치.

### Profiles
39. `feat: add mod profiles` — 게임별 격리된 모드 구성/전환 전략.
40. `feat: add profile management interface` — 프로필 생성·복제·전환·삭제 UI.

### Modpacks
41. `feat: define ModMesh pack manifest` — 버전 있는 모드팩 규격과 검증.
42. `feat: add local modpack import and export` — 로컬 Export/Import 및 재현 계획.
43. `feat: add modpack diff and version planning` — 모드팩 변경 내역 계산.

### Server, Accounts, Sharing
44. `feat: initialize provider independent ModMesh server` — FastAPI + PostgreSQL 기반 API/마이그레이션.
45. `feat: add server authentication and ownership` — 회원가입/로그인, 이메일 확인·복구, 세션, 소유권 정책.
46. `feat: add account registration and login interface` — **기존 계획에 추가된 커밋:** 계정 UI·게스트 모드·내 팩 목록.
47. `feat: add modpack publishing and versioning` — 게시·소유자 관리·불변 버전.
48. `feat: add share code system` — 공유 코드/접근 통제/회수.
49. `feat: add remote modpack import and sync` — 코드 기반 Import/버전 차이 동기화.
50. `build: add standalone and container server deployment` — Self-hosting, Docker/Standalone, 운영 가이드.

### Expansion and Release
51. `feat: add custom mod source` — 허용된 로컬/외부 파일 커스텀 출처.
52. `feat: add ARK Survival Evolved support` — **Evolved Steam판** 어댑터와 Workshop 구독/설치 상태 연동·공식 인계.
53. `chore: prepare ModMesh initial release` — 전체 회귀·사용성·Windows 배포·서버 운영 점검.

## 13. Work로 넘길 때 사용할 작업 지시문

아래 문구를 Work 채팅에 이 파일과 함께 전달한다.

> 프로젝트: **ModMesh**  
> 저장소: https://github.com/Koreapanda4444/ModMesh  
> 첨부한 「ModMesh 최종 개발계획서」를 개발 기준으로 삼고, **실제 GitHub 저장소 상태가 더 최신이면 이를 우선 검증**해주세요.  
> 기술 스택은 **Python + PySide6 + SQLite(로컬), FastAPI + PostgreSQL(서버)** 입니다. Rust/Tauri/React 초안은 대체했습니다.  
> 지원 범위는 Palworld Steam, Minecraft Java Edition, **ARK: Survival Evolved Steam**이며 Ascended는 제외합니다.  
> Game Adapter / Source Adapter / Core를 분리하고 계정 없이도 로컬 기능이 작동하게 하세요. 회원가입·로그인, 모드팩 게시·공유 및 자가 호스팅 서버까지 포함하여 완성도를 목표로 개발하세요.  
> **Commit 01부터 지정한 범위까지 기능을 하나씩 실제 구현→빌드/실행 및 안전성 검증→오류 수정→커밋→푸시**하세요. 미래 커밋을 미리 몰아서 구현한 뒤 커밋만 쪼개지 마세요. 불필요한 테스트 파일, fixture, 샘플, LICENSE를 기본 생성하지 말되 핵심 안전성 검증은 예외적으로 필요한 만큼 수행하세요.  
> GitHub Actions가 깨졌으면 다음 단계로 진행하지 말고 원인부터 수정하세요. 실제로 검증하지 못한 항목은 미검증으로 보고하고, 외부 플랫폼 API/권한 제한을 우회하지 마세요.  
> 각 단계 완료 보고는 **계획 번호, 실제 커밋 SHA/링크, 구현한 내용, 검증 결과, 외부 인증·환경 블로커, 다음 작업**으로 간결하게 남기세요.  
> **이 문구와 파일을 받았다는 사실만으로 개발을 시작하지 말고, 사용자가 별도로 시작 범위를 지정했을 때 착수하세요.**

## 14. 남은 외부 결정 및 현실적 블로커

프로그램 기획 자체는 이 문서로 시작 가능하다. 그러나 실제 전체 제품을 완성·공개하려면 다음은 별도로 확인/제공되어야 한다.

- **Nexus Mods:** 공개 앱 등록 여부 및 다운로드 허용 범위/사용자 인증 방식. 개인 API 키를 공개 앱에 넣지 않음.
- **CurseForge:** API 키 승인 및 승인된 다운로드 흐름. 승인 전에는 제한을 UX에 반영.
- **공개 서버:** VPS/Oracle 등 서버 제공처, 도메인, HTTPS, 메일 발송 계정, PostgreSQL 백업. 로컬 개발에 오라클이 즉시 필요한 것은 아님.
- **Qt/PySide6 배포 라이선스:** 상용/비공개 배포 계획에 따른 LGPL 등의 충족 방식 확인. 프로젝트 자체 라이선스는 사용자가 별도로 결정할 때까지 임의로 부여하지 않음.
- **실게임 검증 환경:** Palworld, Minecraft Java, ARK Survival Evolved 설치 및 실사용 검증에 접근 가능한 환경. 없으면 해당 부분을 완료로 가장할 수 없음.

그 외 UI의 세부 색상·아이콘·정렬, 세부 폴더 배치, 라이브러리 소버전은 개발하면서 판단해도 된다.

## 15. 참고 공식 자료

- Qt for Python / PySide6: https://doc.qt.io/qtforpython-6/
- Qt licensing: https://doc.qt.io/qt-6/licensing.html
- Modrinth API: https://docs.modrinth.com/api/
- Nexus Mods API policy: https://help.nexusmods.com/article/114-api-acceptable-use-policy
- Nexus Mods API docs: https://api-docs.nexusmods.com/
- CurseForge API key application: https://support.curseforge.com/support/solutions/articles/9000208346-about-the-curseforge-api-and-how-to-apply-for-a-key
- ARK: Survival Evolved Steam Workshop: https://steamcommunity.com/workshop/browse/?appid=346110

