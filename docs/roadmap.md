# 개발 로드맵

기준: [최종 Python 개발계획서](development-plan.md). 완료는 실제 검증 범위에 한합니다.

| 번호 | 상태 | 커밋 | 내용 |
|---|---|---|---|
| 01 | 완료 | `chore: initialize ModMesh project` | 저장소 초기화, README 및 최소 구조. |
| 02 | 완료 | `build: configure desktop and server toolchains` | Python 패키지/가상환경, PySide6/FastAPI 개발도구, 기초 실행/빌드/검증. |
| 03 | 완료 | `feat: add application configuration` | 앱·서버 설정, 환경별 구성, 검증. |
| 04 | 완료 | `feat: add logging and error handling` | 공통 구조화 오류/로그, 보안 마스킹. |
| 05 | 완료 | `feat: initialize local metadata storage` | SQLite 스키마·마이그레이션 및 저장 계층. |
| 06 | 완료 | `feat: define game and mod domain models` | 게임·모드·설치/버전/파일 모델. |
| 07 | 완료 | `feat: add canonical mod identity` | 출처 간 동일 모드 식별 근거. |
| 08 | 완료 | `feat: add file hash and artifact identity` | SHA-256/Artifact 식별·무결성. |
| 09 | 완료 | `feat: define dependency relationships` | required/optional/incompatible/embedded 관계. |
| 10 | 완료 | `feat: add game adapter architecture` | 독립 Game Adapter 계약 및 Registry. |
| 11 | 완료 | `feat: add source adapter architecture` | Source Adapter/지원 capability/Registry. |
| 12 | 완료 | `feat: implement game discovery engine` | 게임 경로 탐지 및 수동 지정. |
| 13 | 구현 · 실게임 미검증 | `feat: add Palworld game support` | Steam 설치·버전·모드 환경 탐지. |
| 14 | 구현 · 실게임 미검증 | `feat: add Palworld mod discovery` | 실제 모드 위치/형태/기존 파일 안전 탐지. |
| 15 | 예정 | `feat: add shared source network layer` | HTTP/재시도/타임아웃/제한/자격 증명 경계. |
| 16 | 예정 | `feat: add Nexus Mods integration` | 정책 준수 검색·정보·허용 다운로드/인계. |
| 17 | 예정 | `feat: add Modrinth integration` | 검색·버전·로더·의존성·파일. |
| 18 | 예정 | `feat: add CurseForge integration` | 허용되는 API 키/검색·조회·다운로드 인계. |
| 19 | 예정 | `feat: implement unified mod search` | 출처 통합 검색/필터/오류 격리. |
| 20 | 예정 | `feat: add cross-source mod matching` | 동일 모드 후보 매핑. |
| 21 | 예정 | `feat: add identical artifact merging` | 확인된 동일 해시 파일 병합. |
| 22 | 예정 | `feat: add per-game source priorities` | 게임별 출처 우선순위와 fallback. |
| 23 | 예정 | `feat: add mod download manager` | 취소·재시도·진행률·다운로드 큐. |
| 24 | 예정 | `feat: add hash verified mod cache` | 검증 캐시 재사용 및 정리. |
| 25 | 예정 | `feat: add dependency resolver` | 버전 제약/필수 의존성 해결. |
| 26 | 예정 | `feat: add dependency conflict detection` | 충돌·누락·순환·로더 호환성. |
| 27 | 예정 | `feat: add mod installation planner` | 설치 전 파일 변경 계획. |
| 28 | 예정 | `feat: implement safe mod installation` | 설치·파일 기록·백업·복구. |
| 29 | 예정 | `feat: implement safe mod removal` | 추적한 모드 파일만 안전 제거. |
| 30 | 예정 | `feat: protect shared dependencies` | 참조 중인 의존성 제거 차단. |
| 31 | 예정 | `feat: add game library interface` | 게임 라이브러리/선택/경로 UI. |
| 32 | 예정 | `feat: add integrated mod search interface` | 통합 검색 결과/필터/출처 UI. |
| 33 | 예정 | `feat: add mod details and install actions` | 상세/버전/설치 계획/실행 연결. |
| 34 | 예정 | `feat: add installed mod management` | 설치 목록/업데이트/안전 삭제 UI. |
| 35 | 예정 | `feat: add download and operation progress` | 다운로드/설치 진행·오류·복구 UI. |
| 36 | 예정 | `feat: add Minecraft game adapter` | Java Edition 인스턴스 탐지. |
| 37 | 예정 | `feat: add Minecraft loader support` | Fabric/Forge/NeoForge별 규칙. |
| 38 | 예정 | `feat: integrate Minecraft multi-source mods` | 호환성 기반 다중 Source 검색/설치. |
| 39 | 예정 | `feat: add mod profiles` | 게임별 격리된 모드 구성/전환 전략. |
| 40 | 예정 | `feat: add profile management interface` | 프로필 생성·복제·전환·삭제 UI. |
| 41 | 예정 | `feat: define ModMesh pack manifest` | 버전 있는 모드팩 규격과 검증. |
| 42 | 예정 | `feat: add local modpack import and export` | 로컬 Export/Import 및 재현 계획. |
| 43 | 예정 | `feat: add modpack diff and version planning` | 모드팩 변경 내역 계산. |
| 44 | 예정 | `feat: initialize provider independent ModMesh server` | FastAPI + PostgreSQL 기반 API/마이그레이션. |
| 45 | 예정 | `feat: add server authentication and ownership` | 회원가입/로그인, 이메일 확인·복구, 세션, 소유권 정책. |
| 46 | 예정 | `feat: add account registration and login interface` | **기존 계획에 추가된 커밋:** 계정 UI·게스트 모드·내 팩 목록. |
| 47 | 예정 | `feat: add modpack publishing and versioning` | 게시·소유자 관리·불변 버전. |
| 48 | 예정 | `feat: add share code system` | 공유 코드/접근 통제/회수. |
| 49 | 예정 | `feat: add remote modpack import and sync` | 코드 기반 Import/버전 차이 동기화. |
| 50 | 예정 | `build: add standalone and container server deployment` | Self-hosting, Docker/Standalone, 운영 가이드. |
| 51 | 예정 | `feat: add custom mod source` | 허용된 로컬/외부 파일 커스텀 출처. |
| 52 | 예정 | `feat: add ARK Survival Evolved support` | **Evolved Steam판** 어댑터와 Workshop 구독/설치 상태 연동·공식 인계. |
| 53 | 예정 | `chore: prepare ModMesh initial release` | 전체 회귀·사용성·Windows 배포·서버 운영 점검. |
