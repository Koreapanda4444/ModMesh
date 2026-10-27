# 게임 규칙

## 등록과 업데이트

데스크톱의 **게임 규칙**에서 필수 실행 파일의 상대 경로·모드 폴더·확장자·로더를 입력하고 JSON을 생성합니다. 게임 폴더는 선택 사항이며 지정하면 실제 파일과 규칙을 대조합니다. JSON을 편집하거나 외부 JSON을 불러올 수도 있습니다. **변경 없이 검사 → 내용 확인 → 검토한 규칙 승인·등록** 순서로 진행합니다. JSON이나 게임 폴더를 바꾸면 승인을 다시 받아야 합니다. 등록 후 내 게임과 커스텀 출처에서 사용할 수 있습니다.

Python 설치형 CLI에서도 같은 서비스를 사용합니다.

```powershell
modmesh-cli rules check --file game.json --path D:/Games/MyGame
modmesh-cli rules register --file game.json --path D:/Games/MyGame --approve SHA256
modmesh-cli rules list
modmesh-cli games --game my-game
```

`SHA256`은 check 출력의 `digest`로 바꿉니다. 기존 규칙 변경에는 더 큰 `revision`과 check의 `previous_digest`를 `--previous PREVIOUS_SHA256`으로 추가해야 합니다. 다른 프로세스가 그 사이 바꾼 규칙을 덮지 않습니다. 내장 게임은 사용자 규칙으로 교체할 수 없습니다. 커뮤니티 출처도 승인 전에는 비활성 상태이며 내장 > 사용자 > 커뮤니티 순서와 충돌 승인을 따릅니다. 내용 해시는 서명이나 제작자 인증이 아닙니다. 커뮤니티 자동 다운로드는 제공하지 않습니다.

규칙과 수동 경로는 기존 로컬 DB의 별도 namespace에 저장합니다. 기존 DB에 규칙을 등록·업데이트하기 전에는 데이터 폴더의 `backups/rules-*.sqlite3`에 일관된 백업을 남깁니다. 잘못된 규칙은 실행하지 않고 원본을 보존하며 탐색 진단에 알립니다. 규칙의 단순 표시 변경은 기존 규칙 기반 프로필을 잠그지 않지만, 변경된 모드 경로·허용 확장자를 벗어나는 프로필 전환은 차단합니다.

수동 DB 백업은 `modmesh-cli metadata backup --output backup.sqlite3`, 복원은 `modmesh-cli metadata restore --input backup.sqlite3 --output restored.sqlite3`입니다. 기존 DB를 덮지 않으므로 복원 결과를 확인한 뒤 별도로 설정을 변경합니다. DB 백업은 게임별 `.modmesh-state-v1`의 파일·프로필 blob이나 캐시를 포함하지 않습니다. 해당 게임 폴더와 자료도 함께 보존해야 합니다.

실행 정의는 현재 검증·저장만 합니다. 로더 설치·자동 게임 실행은 제공하지 않습니다. BepInEx는 알려진 로더 DLL의 존재와 직접 추가한 DLL의 기본 PE 헤더만 검사하며 API/게임 호환성은 미검증입니다. UE4SS는 선택한 모드 루트 안의 `Scripts/main.lua` 또는 `dlls/main.dll`과 `enabled.txt` 배치를 검사합니다. 모드 코드를 ModMesh가 실행하지 않습니다. Minecraft의 인스턴스/Java 프로세스/JAR 호환성 검사는 내장 브리지만 사용합니다.

## 형식

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
