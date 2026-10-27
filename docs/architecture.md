# 범용 규칙 전환

## 기준과 호환성

2026-10-27 기준 `64ce6a2` / `0.1.0rc1`에서 시작합니다. 기준 Actions `40215878604`의 Windows·Ubuntu·PostgreSQL·Container·Windows 패키지가 모두 성공했습니다. 0.2 작업은 기존 설치·복구 엔진과 데이터 형식을 유지하며 단계적으로 연결합니다.

| 기존 계약 | 유지할 경계 |
|---|---|
| `GameAdapter` / `GameRegistry` | Minecraft 인스턴스·JAR 검사, Palworld UE4SS, ARK Workshop 특수 동작은 브리지로 보존 |
| `InstallationPlanner` / install·remove·profiles | fingerprint 승인, 파일 소유권·해시·실행 중 차단·중단 복구를 우회하지 않음 |
| 로컬 SQLite schema 1 | namespace별 레코드를 유지, 파손·미래 schema는 거부; 변환 전 일관된 백업 필요 |
| Profile / PackManifest schema 1 | 기존 파일과 ID를 유지; 새 workspace ID와 동일시하거나 자동 재작성하지 않음 |
| FastAPI / Alembic `0004_shares` | PostgreSQL 운영 계약·계정·불변 개정·공유 코드 유지 |
| Windows portable / Inno Setup | 독립 실행, 사용자별 설치, 제삼자 고지·소스·제거 시 사용자 데이터 보존 유지 |

게임 탐지의 자동/수동 경로 처리는 CLI와 GUI가 공통 application service를 사용합니다. 규칙은 검증한 데이터이며 Python 코드·Shell 훅이 아닙니다. 규칙에 허용된 기능과 현재 설치본에서 충족한 조건을 구분합니다. 현재 계층은 `apps`(사용 사례/표시), `core`(계획·검증·전략), `adapters`(플랫폼별 I/O), `domain`(값과 형식), `storage`(영속성)입니다.

실게임 모드 로딩, Nexus 인증 다운로드, CurseForge 승인 API, 외부 SMTP와 실제 Workshop 계정 상태는 미검증입니다. 규칙이나 CLI 검사 성공이 이 범위를 검증한 것으로 간주되지 않습니다. 기존 게임 지원 범위는 README를 따릅니다.
