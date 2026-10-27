# ModMesh

Python 3.12, PySide6, FastAPI 기반 데스크톱 모드 매니저입니다.

현재 버전은 `0.1.0rc1` 릴리스 후보입니다. Windows x64 포터블 ZIP·사용자별 설치 EXE와 체크섬·의존성 고지·대응 소스는 성공한 [Quality Actions 실행](https://github.com/Koreapanda4444/ModMesh/actions/workflows/ci.yml)의 `ModMesh-windows-x64` Artifacts에서 받습니다. Python 없이 실행하며, 설치·업데이트·제거와 검증 범위는 [Windows 배포 문서](docs/windows.md)를 참고하세요.

현재 CLI에서 게임 탐지, 출처별 검색, 버전 조회, 필수 의존성 버전 해결, 해시 검증 다운로드와 캐시를 사용할 수 있습니다. 데스크톱은 게임 라이브러리·자동/수동 경로 등록·환경 상세와 마지막 게임 복원을 제공합니다. 모드 탐색은 게임·버전·로더·출처·정렬 필터, 출처별 제한 안내와 다음 페이지를 지원합니다. 프로젝트 상세에서 버전·파일·의존성을 조회하고 변경 계획을 검토·승인해 설치할 수 있습니다. 서버는 DB 준비 상태·API 정보와 PostgreSQL/Alembic 마이그레이션을 제공합니다. 모드 설치 전 변경 계획과 승인 적용·백업·중단 복구를 사용할 수 있습니다. 설치된 모드 화면에서 관리/외부 파일·사용자 변경·필수 의존성을 구분하고 다른 호환 버전 조회와 제거 계획을 실행할 수 있습니다. 다운로드/작업 화면은 대기열·전송/적용 진행·취소·조회 재시도·영속 저널과 중단 복구를 제공합니다. 파일 변경 작업은 새 계획 검토 후 다시 적용합니다. 게임별 로컬 프로필 Core는 관리 모드 파일의 구성 저장·복제·전환과 원본 복원·중단 복구를 제공합니다. 프로필 화면에서 생성·복제·이름 변경·삭제와 전환 계획 검토를 사용할 수 있으며, 상단에서 활성 프로필을 선택합니다. 모드팩 화면은 프로필 Manifest Export와 로컬 파일 검증·출처 재확인·설치 계획 Import를 제공합니다. 서버 계정 API는 회원가입·메일 확인·로그인·로그아웃·전체 세션 폐기·비밀번호 복구/변경·이메일 변경과 소유자 검사를 제공합니다. 계정 화면에서 서버 선택·가입·로그인·복구·세션 관리를 사용할 수 있고, 온라인 팩 화면에서 게시·버전·공개 범위를 관리합니다.

## 설치 및 실행

Windows PowerShell 기준입니다.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -c requirements.lock ".[desktop,server,dev]"
.venv\Scripts\modmesh
.venv\Scripts\modmesh-server
```

서버 기본 주소는 `http://127.0.0.1:8731/health`입니다. Linux/macOS 데스크톱 제품 지원과 실제 게임 로딩은 아직 검증하지 않았습니다.

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

`discover --game palworld --path <경로>`는 검증된 수동 게임 경로를 저장합니다. Minecraft는 `--game minecraft`로 기본 게임 폴더 또는 Prism/MultiMC 인스턴스 폴더를 등록합니다. 여러 프로필이 같은 폴더를 다른 버전으로 사용하면 버전을 임의 선택하지 않습니다. `download`에는 `versions`가 반환한 정확한 버전 ID를 사용하며, 파일이 모호하면 `--file`로 선택합니다. `--output`을 생략하면 캐시 경로를 반환하고, 지정하면 독립된 복사본으로 내보냅니다. `download` 명령은 파일 준비만 수행합니다.

`resolve`는 `download`와 같은 게임·출처·프로젝트·버전 선택 인자를 받아 필수 의존성을 해결합니다. 정확한 출처 버전 ID와 명시된 SemVer/NPM 범위를 지원하며, 여러 모드의 조건이 맞지 않으면 다른 후보를 탐색합니다. 선택 모드 버전은 고정되고 선택 의존성은 자동 설치하지 않습니다. `solved`와 `metadata_complete`는 각각 제공된 필수 조건의 해결 여부와 의존성 정보의 확인 여부입니다.

의존성 결과에는 누락·버전 불일치·순환·명시적 비호환·게임 버전/로더 충돌 분석도 포함됩니다. 확인되지 않은 의존성·호환성 정보는 경고로 보존하며, 차단 충돌이 있으면 CLI가 실패 상태를 반환합니다.

`plan`은 같은 모드 선택 인자에 `--path <게임 경로>`와 필요한 경우 `--rule <규칙 ID>`, `--archive-prefix <압축 내부 폴더>`를 받아 설치 전 변경 계획과 승인용 fingerprint를 반환합니다. 필요한 파일은 검증 캐시에 준비하며 게임 파일은 변경하지 않습니다. 기존 파일·백업 필요 여부·충돌과 미검증 조건을 표시하고, ZIP 경로 탈출·링크·중복/대소문자 충돌·과도한 압축 해제를 차단합니다. Palworld는 명시적으로 선택한 Pak/Blueprint 또는 알려진 UE4SS 패키지 배치를 구분하며, UE4SS는 로더가 탐지된 경우에만 활성 표식 파일을 계획합니다.

설치하려면 표시된 변경 계획·경고·fingerprint를 검토한 뒤 같은 `plan` 명령에 `--apply <fingerprint>`를 추가합니다. 계획이 바뀌면 새 검토가 필요합니다. 외부 파일 교체는 `--replace-existing`으로 계획에 명시한 경우만 허용하고 원본을 백업합니다. `installed --game <ID> --path <경로>`로 기록을 조회하고 `recover`로 중단 작업의 복구를 시도합니다. 사용자 수정 파일은 보존하며, 복구가 막히면 `recovery_required`와 남은 문제를 표시합니다. 탐지된 게임/인스턴스 프로세스가 실행 중이면 파일 변경을 차단합니다. 적용 중 게임을 시작하지 않아야 하며 해시 일치가 모드 코드의 안전성을 보장하지는 않습니다.

`remove --game <ID> --path <경로> --source <출처> --project <ID>`로 제거 계획을 검토하고 `--apply <fingerprint>`로 실행합니다. 소유권·해시·파일 식별이 유지되는 파일만 제거하며, 교체 전 백업이 있으면 원본을 복원합니다. 외부 파일·공유 파일·사용자 수정 파일은 보존합니다. 다른 설치 모드가 필수 의존성으로 참조하면 제거를 차단합니다. 의존 모드도 함께 제거하려면 `--also <출처>:<프로젝트 ID>`로 명시적으로 포함해 전체 계획을 검토합니다. 선택하지 않은 불필요 의존성을 자동 제거하지 않습니다. 파일명이 달라지는 업데이트는 먼저 제거 계획을 적용한 뒤 새 설치를 검토해야 합니다.

## 게임 및 출처

| 대상 | 현재 범위와 검증 상태 |
|---|---|
| Palworld Windows Steam | 설치 경로·Steam build ID·Pak/UE4SS 모드 탐지. 실제 게임 설치와 모드 로딩은 미검증. |
| Minecraft Java | 공식 런처·Prism/MultiMC 인스턴스와 Fabric/Forge/NeoForge 로더 탐지. JAR 내부 조건 검사·설치·제거 보호. 실제 게임 로딩은 미검증. |
| ARK Evolved Windows Steam | Steam 설치·build ID·로컬 모드 상태 탐지와 공식 Workshop 연결. 실제 구독 상태·게임 로딩은 미검증. |
| Modrinth | Minecraft Java 모드 검색·버전·로더·의존성·파일 조회. 실제 다운로드와 캐시 재사용 검증. |
| Nexus Mods | 공개 인기 목록과 공식 사이트 인계. 전체 카탈로그 검색에는 제한이 있으며, 인증 상세·다운로드 API는 미검증. |
| CurseForge | 승인 API 키와 배포 권한을 사용하는 Minecraft Java 모드 연동. 승인 계정 API·실다운로드는 미검증. |

Minecraft는 확인된 단일 로더와 게임 버전이 있는 클라이언트 인스턴스의 JAR만 `mods`에 설치합니다. 로더 설치·런처 실행·설정/세이브 수정은 수행하지 않습니다. Fabric의 확장 SemVer·시험판·와일드카드와 숫자형 Maven 범위를 검사하며 해석할 수 없는 필수 조건은 차단합니다. 여러 JAR에 포함된 Fabric 공용 모드는 내장 JAR의 SHA-256과 메타데이터까지 같을 때 한 번 검증합니다. 서로 다른 내장 후보의 로더 선택은 미검증으로 차단합니다. 선택한 런처의 Java 런타임과 일부 NeoForge FML 언어 로더 범위는 경고로 미검증 표시합니다. 실제 Modrinth 다운로드 검증은 실행 환경의 OS DNS 제한으로 공개 DoH 응답을 주입해 수행했으며, 제품 기본 경로는 OS DNS입니다.

Minecraft 다중 출처는 선택 인스턴스의 게임 버전·로더로 조회하고 설치 계획에서 다시 검증합니다. 상세 화면에서 정확한 프로젝트 ID로 출처 연결을 검토·해제할 수 있습니다. 연결만으로 파일을 같은 것으로 취급하지 않으며, 확인된 호환 맥락과 SHA-256/SHA-512 일치가 있을 때만 허용된 출처로 대체 다운로드합니다. API 키·배포 권한이 없거나 약한 해시만 제공되면 해당 대체 후보를 사용할 수 없다고 표시합니다.

프로필은 게임 설치별로 저장하며 전환 계획의 fingerprint를 승인해야 실제 파일을 변경합니다. 외부 모드는 프로필 간 공유되고, 사용자 수정·외부 재사용 파일·서로 다른 게임 버전/로더는 자동 전환하지 않습니다. Minecraft `options.txt`는 명시적으로 선택한 경우에만 로컬 프로필에 포함합니다. 세이브와 인증 정보는 수집하지 않습니다. 실제 게임 실행에서의 프로필 로딩은 미검증입니다.

모드팩 Manifest schema 1은 팩 ID·불변 개정 번호·게임/판본/플랫폼/로더 맥락과 정확한 출처·버전·파일 ID, 해시, 의존성, 설치 규칙·압축 prefix를 기록합니다. JSON은 최대 2 MiB·128개 모드이며 중복 키·알 수 없는 필드·경로 탈출을 거부합니다. 다운로드 URL·개인 경로·인증 필드는 포함하지 않습니다. 설정은 경로·해시만 포함하며 실제 설정 내용은 제외합니다. 강한 출처 해시나 의존성 정보가 없으면 재현성 경고를 유지합니다. 기존 설치 중 규칙/prefix가 기록되지 않았던 항목은 Import에서 자동 추측하지 않습니다.

로컬 모드팩 Import는 선택한 게임 환경과 일치해야 합니다. 출처에서 정확한 버전·파일·해시·의존성을 다시 확인하고 허용된 원본만 준비합니다. 설치 파일이 없는 출처나 불완전한 Manifest는 차단합니다. 다른 관리 모드가 있는 구성은 먼저 빈 프로필로 전환해야 하며, 모드팩 화면의 변경·버전 계획은 현재 구성을 보존한 뒤 새 비활성 프로필을 준비하고, 승인 시 전체 구성을 전환합니다. 추가·삭제·업데이트·설정 메타데이터 차이를 비교할 수 있습니다. 로컬 팩 ID와 개정 번호의 내용은 변경 불가하며 같은 번호에 다른 내용을 저장하려면 새 개정이 필요합니다. Export는 기존 파일을 덮지 않습니다. 설정 내용과 외부 모드는 재현 대상에 포함하지 않습니다.

## 설정

내장 게임 외에 선언형 JSON 규칙으로 게임을 추가할 수 있습니다. 데스크톱의 **게임 규칙**에서 게임·모드 폴더·로더·실행 정의를 입력해 JSON을 만들거나 불러오고, 변경 없이 검사한 뒤 명시적으로 승인합니다. CLI는 `modmesh-cli rules check --file game.json`이 반환한 해시를 `rules register --file game.json --approve SHA256`에 전달합니다. 경로를 함께 등록하려면 두 명령에 `--path <게임 폴더>`를 지정합니다. 개정 업데이트는 더 큰 revision과 `--previous PREVIOUS_SHA256`이 필요합니다. `rules list`와 `games`에서 출처·승인 상태와 기능별 지원을 확인합니다. [규칙 계약과 사용법](docs/game-rules.md)을 참고하세요.

규칙 기반 파일/ZIP/경로 매핑과 BepInEx·UE4SS 배치는 기존 승인·파일 보호·제거·프로필 엔진을 사용합니다. 로더는 별도 설치가 필요합니다. 내장 게임 덮어쓰기와 위험 경로·명령을 거부하며, 잘못 저장된 규칙은 원본을 보존하고 탐색 진단에 표시합니다. **실행 정의는 현재 저장·검증만 하며 게임 실행 기능은 아직 제공하지 않습니다.** 새로운 독립 팩 workspace·잠금 파일·동기화·Play 통합은 이후 단계입니다.

클라이언트는 `--config <TOML>` 또는 `MODMESH_CONFIG`로 설정을 지정합니다. 기본 파일은 `%APPDATA%/ModMesh/config.toml`이며, 없으면 기본값을 사용합니다. `[client]`에는 `data_dir`, `cache_dir`, `cache_max_bytes`, `server_url`, `log_level`과 `source_priorities`를 지정할 수 있습니다. `MODMESH_DATA_DIR`, `MODMESH_CACHE_DIR`, `MODMESH_CACHE_MAX_BYTES`, `MODMESH_SERVER_URL`, `MODMESH_LOG_LEVEL` 환경 변수가 우선합니다.

서버는 `--config` 또는 `MODMESH_SERVER_CONFIG`의 `[server]` 설정과 `MODMESH_BIND_HOST`, `MODMESH_BIND_PORT`, `MODMESH_ENV`, `MODMESH_DATABASE_URL`, `MODMESH_LOG_LEVEL`을 사용합니다. 개발 모드의 DB 없는 health는 `database: disabled`로 표시합니다. 운영 모드는 PostgreSQL URL이 필수이며, 먼저 `modmesh-server --migrate`를 실행하고 서버를 시작합니다. 마이그레이션은 서버 시작 시 자동 실행하지 않습니다. `--check`는 지정한 DB를 검사하며, DB를 지정하지 않은 개발 검사는 임시 SQLite에서 수행합니다.

출처 API 키는 `MODMESH_NEXUS_API_KEY`, `MODMESH_CURSEFORGE_API_KEY` 환경 변수로 전달합니다. 인증·배포 제한이 있으면 공식 사이트로 인계하며 직접 다운로드 권한을 우회하지 않습니다.

캐시 기본 용량은 콘텐츠 5 GiB입니다. 재사용 전 해시를 검사하고 용량 초과 시 오래 사용하지 않은 항목부터 정리합니다. 기존 출력 파일과 소유 근거 없는 캐시 파일은 보존합니다. 미등록 파일과 인덱스·잠금 파일은 용량 집계에서 제외됩니다. 출처 해시가 없으면 `unverified`, MD5/SHA-1만 있으면 `weak`로 표시합니다. 파일 게시에는 하드 링크와 안정적인 파일 식별을 지원하는 파일시스템이 필요합니다.

서버 계정은 15–128자 비밀번호와 Argon2id를 사용하며, 이메일 확인 전에는 로그인할 수 없습니다. 확인·복구 코드는 만료·단일 사용이고 DB에는 세션/코드의 SHA-256만 저장합니다. 복구·비밀번호/이메일 변경은 기존 세션을 폐기합니다. 가입·로그인·복구는 DB 기반 요청 제한을 사용합니다. 검증 오류 응답에 비밀번호 원문을 반환하지 않습니다.

메일은 `MODMESH_SMTP_HOST`, `MODMESH_SMTP_PORT`, `MODMESH_SMTP_FROM`, `MODMESH_SMTP_USERNAME`, `MODMESH_SMTP_PASSWORD`, `MODMESH_SMTP_TLS`로 설정합니다. 사용자명·비밀번호는 환경 변수로만 받습니다. 기본은 STARTTLS이며 TLS를 끌 수 있는 대상은 loopback뿐입니다. 메일 미설정 시 가입·코드 발급은 사용 불가로 반환합니다. SMTP 프로토콜과 전체 계정 흐름은 로컬 수신기에서 검증하며, 실제 외부 메일 배달은 별도 SMTP 계정으로 확인해야 합니다. `MODMESH_SESSION_SECONDS`는 5분~30일 범위이며 기본은 7일입니다.

## 개발 검증

ARK: Survival Evolved는 `ark-evolved`로 Windows Steam판을 탐지합니다. Steam app ID 346110과 설치 Manifest·실행 파일을 대조하며 Ascended와 다른 판본은 지원하지 않습니다. Steam Workshop 화면에서 `.mod`·게임 모드 폴더·Steam 다운로드 폴더·설치 기록·ActiveMods 설정을 구분해 조회하고 공식 Workshop 구독 페이지를 엽니다. 로컬 기록은 계정의 현재 구독 여부를 보장하지 않으므로 구독·업데이트·제거는 Steam에서 확인합니다. Steam 소유 파일과 설정·세이브는 읽기 전용이며 Workshop 파일 직접 다운로드·복사 설치·프로필 전환은 제공하지 않습니다. 실제 ARK 실행과 계정 구독 상태 확인은 실게임 환경에서 추가 검증해야 합니다.

커스텀 출처 화면에서 권한 있는 로컬 파일 또는 공개 HTTPS 파일을 현재 게임·버전·로더에 등록할 수 있습니다. HTTPS에는 파일명과 SHA-256이 필수이며 인증정보·쿼리 문자열·사설 주소는 받지 않습니다. 모드 탐색에서 `custom` 출처로 검색한 뒤 기존 설치 규칙·계획·승인을 사용합니다. 등록 삭제는 목록에서만 제거하며 원본과 설치 파일을 지우지 않습니다. 로컬 파일은 등록 시 해시를 계산하고 복사 시 다시 확인합니다. 경로·URL은 Manifest에 넣지 않으며 다른 PC는 같은 게임·해시의 사용 가능한 출처를 다시 등록해야 합니다. 의존성 미확인은 표시하며, 임의 설치 스크립트를 실행하지 않습니다.

별도 서버는 Python 명령 또는 `Dockerfile`·`compose.yml`로 실행할 수 있습니다. DB 준비·마이그레이션·비관리자 실행과 비밀번호 파일 전달, HTTPS·SMTP·백업·복원 절차는 [서버 운영 문서](docs/server.md)에 있습니다. 실제 공개 도메인과 외부 메일 계정은 운영자가 설정해야 합니다.

모드팩 화면의 공유 코드 Import는 선택 서버에서 Manifest·해시를 확인한 뒤 기존 변경 계획에 연결합니다. 신규 설치는 Import 계획, 기존 구성 전환은 팩 변경·버전 계획을 사용하며 승인 전에는 게임 파일을 변경하지 않습니다. 성공한 적용은 서버·설치·활성 프로필별로 기록합니다. 원격 업데이트 확인은 이 기록을 기준으로 차이를 보여주고, 비공개/코드 공개 팩은 유효한 코드를 다시 입력해야 합니다. 코드는 기록에 저장하지 않습니다. 서버 변경, 개정 내용 변경과 이전 개정으로의 후퇴는 자동 적용하지 않습니다.

온라인 팩에서 공유 코드를 발급·회수할 수 있습니다. 코드는 144비트 무작위 값이며 DB에는 해시만 저장하고 발급 직후에만 표시합니다. 특정 개정 고정 또는 최신 개정, 만료 시간과 최대 조회 횟수를 지정합니다. 코드는 Manifest 조회 권한만 부여하며 수정 권한을 주지 않습니다. 비공개 전환·게시 중단 시 기존 코드는 영구 회수됩니다. 조회 횟수는 코드 확인 요청마다 차감하며, 회수 전에 이미 내려받은 파일까지 삭제하지는 않습니다.

온라인 팩 화면에서 로컬 Manifest 신규 게시·새 개정 게시·내 팩/버전 조회와 공개 범위·설명·게시 중단을 관리합니다. API는 소유권을 서버에서 확인하며 Manifest 바이너리나 외부 출처 인증정보는 중계하지 않습니다. 개정은 덮어쓸 수 없고 최신 개정 기준의 충돌 검사를 통과해야 추가됩니다. 비공개와 공유 코드 공개 팩은 소유자만 직접 조회할 수 있습니다. 공개 팩도 게시 중단하면 외부 조회가 차단되며, 기존 버전은 소유자 이력으로 보존합니다.

계정 화면에서 서버 URL 선택, 회원가입·메일 확인·로그인·복구·비밀번호/이메일 변경·세션 폐기를 사용할 수 있습니다. 서버마다 세션을 분리하고 HTTP 리디렉션으로 인증정보를 전달하지 않습니다. 로그인 유지를 선택하면 OS 보안 저장소에 세션을 저장하며, 저장소를 사용할 수 없으면 현재 실행의 메모리에만 유지합니다. 비밀번호는 저장하지 않습니다. 게스트와 서버 연결 실패 상태에서도 로컬 기능을 사용할 수 있습니다. 내 팩 목록은 선택한 서버가 팩 API를 제공할 때 조회합니다.

```powershell
.venv\Scripts\python -m ruff check src tools packaging
.venv\Scripts\python -m ruff format --check src tools packaging
.venv\Scripts\python -m mypy
.venv\Scripts\python -m pip wheel --no-deps --no-build-isolation -w dist .
.venv\Scripts\modmesh --check
.venv\Scripts\modmesh-server --check
.venv\Scripts\python tools/verify_safety.py
```

화면 없는 환경은 `QT_QPA_PLATFORM=offscreen`을 설정합니다. CI는 Windows·Ubuntu에서 빌드·설치·기본 실행·파일 보호·SQLite 마이그레이션을 검증하고, 별도 PostgreSQL 컨테이너에서 마이그레이션 반복 적용과 API 준비 상태를 검사합니다.

Windows 패키지는 Python 3.12 x64에서 `pip install -c requirements.lock ".[desktop,package]"` 후 `python tools/build_windows.py`로 빌드합니다. Inno Setup 6의 `ISCC.exe`와 Qt 공식 소스 배포처에 대한 네트워크 접근이 필요합니다. 출력 경로는 비어 있어야 합니다. `python tools/verify_windows_package.py`는 Python 검색 경로를 제거한 자식 프로세스에서 포터블·설치형의 네이티브 Qt 화면·탐색·재실행·같은 버전 재설치·제거와 사용자 파일 보존을 검사합니다. Quality의 다른 검사가 모두 성공한 뒤 빌드하며, 패키지 검사까지 통과해야 Artifacts를 업로드합니다. 실제 사용자 PC·게임·외부 인증 API·외부 메일 배달 검증을 대체하지 않습니다.
