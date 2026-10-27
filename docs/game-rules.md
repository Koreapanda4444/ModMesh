# 게임 규칙

규칙은 JSON schema 1입니다. Python 모듈이나 설치 스크립트가 아닙니다. 등록·조회·안전한 파일 배치를 선언하며, 실제 게임에서의 모드 로딩을 보증하지 않습니다. 기존 Minecraft/Palworld/ARK의 특수 검증은 유지합니다.

| 필드 | 계약 |
|---|---|
| `schema_version`, `revision` | 각각 1과 양의 정수. 같은 게임의 변경은 새 revision과 이전 내용 해시의 승인 필요 |
| `game_id`, `name`, `edition`, `platforms` | 안정된 소문자 ID, 표시 이름, 판본, `windows`/`linux`/`macos` 목록 |
| `store_ids` | 선택적인 `steam` 숫자 App ID. 임의 URL 없음 |
| `detect` | 게임 루트 기준 `required_files`, `steam_manifest_required` |
| `game_version` | `strategy`: `unknown` 또는 `steam-build`. 게임 버전과 규칙 revision은 별개 |
| `loader` | `strategy`: `none`/`bepinex`/`ue4ss`/내장 `legacy-minecraft`, `markers`: 로더 파일 경로 |
| `mod_roots` | `root_id`, 게임 안의 상대 `path`, 표시/검사 `layout` |
| `install_strategies` | `strategy_id`, `kind`, `root_id`, 명시적 `extensions`, `mappings` |
| `mod_types` | 지원 파일 종류의 소문자 식별자 목록 |
| `profile_scope` | `none` 또는 `managed-files`. 세이브·사용자 파일을 자동 수집하지 않음 |
| `launch` | `strategy`: `manual`/`steam`/`executable`, `executable`, `app_id`, `arguments`. 실행 호출은 별도 단계에서 제공 |
| `capabilities` | 실제 구현된 discovery/version/mod_discovery/install_rules/removal_rules/profiles/workshop_handoff의 부분집합 |

파일 배치 종류는 `file`(단일 파일), `archive`(제한된 ZIP), `mapped`(ZIP prefix 매핑), `plugin`(로더가 필요한 파일/ZIP), `readonly`(파일 변경 없음)입니다. prefix 매핑은 `source_prefix`, `target_prefix` 쌍이며 대상은 항상 선택한 모드 루트 안입니다. 실제 설치는 기존 변경 계획·fingerprint 승인·파일 소유권·해시·복구 엔진을 사용합니다.

루트 밖 경로·경로 변수·와일드카드·스크립트 훅·임의 URL·실행 인터프리터·Shell 인수·세이브/Steam 소유 영역을 허용하지 않습니다. 로더를 설치하거나 바이너리를 패치하지 않습니다. 실행 정의를 등록해도 실행 기능이 지원됐다는 뜻은 아닙니다. 로더 파일 탐지는 게임 내부 호환성 증거와 구분합니다.
