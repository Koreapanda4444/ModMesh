# 검증 범위

- 01~12: 패키지 빌드, Ruff, 엄격한 mypy, Qt 이벤트 루프, HTTP health, SQLite 영속성/롤백/동시 접근, 식별·해시·어댑터 계약과 탐지 엔진 직접 실행을 검증했습니다. Windows/Ubuntu CI는 각 푸시에서 별도로 확인합니다.
- 13 Palworld: 임시 Steam 메타데이터에서 여러 라이브러리, App ID, 빌드 ID, 수동 경로, 잘못된 파일/경로 거부를 검증했습니다. **실제 Palworld Steam 설치와 게임 실행은 미검증**입니다. 의미상 게임 버전을 Steam build ID로 추측하지 않습니다.
- Windows 독립 실행 파일/설치 패키지의 배포 검증은 아직 수행하지 않았습니다. 현재 CI는 Python 패키지와 offscreen Qt 실행을 검증합니다.

- 14 Palworld 모드 탐지: `~mods`, `LogicMods`의 Pak/IoStore 파일 묶음, 두 UE4SS Mods 배치, 설정상의 활성 여부, 로더 DLL 존재, 외부 파일·심볼릭 링크 보호를 임시 디렉터리에서 검증했습니다. 파일은 읽기 전용이며 전부 외부 설치로 분류합니다. 루트의 게임 기본 Pak이나 사용자 지정 중앙 UE4SS 작업 폴더는 자동으로 모드라고 분류하지 않습니다. **실게임에서의 실제 로딩·호환성은 미검증**입니다.

UE4SS 배치/활성 설정 근거: [공식 설치 문서](https://docs.ue4ss.com/installation-guide.html), [공식 모드 설치 문서](https://docs.ue4ss.com/guides/installing-a-c%2B%2B-mod.html). DLL·파일 존재와 설정의 활성 값은 실행 중 로딩 성공을 의미하지 않습니다.

- 15 공통 HTTP: 실제 Modrinth HTTPS JSON 응답(로더 29개)을 확인했습니다. 주입한 HTTP transport로 재시도·잘못된 JSON·크기 제한·타임아웃·출처/인증 헤더 격리·긴 Retry-After 조기 재시도 방지를 검증했습니다. 소스 API 키는 환경 변수에서만 읽고 로그 마스킹에 등록합니다.

- 16 Nexus: 최신 [공식 v3 명세](https://api.nexusmods.com/openapi.yaml)의 공개 trending feed에서 실제 Palworld 프로젝트 3개를 조회했습니다. 전체 카탈로그 텍스트 검색과 버전/로더 필터로 가장하지 않고 제한된 feed임을 결과에 표시하며 공식 사이트로 인계합니다. 상세·파일은 공식 문서가 연결하는 v1 API를 사용합니다. API 키/계정 권한에 따른 메타데이터 파싱과 다운로드/인계 분기는 주입 transport로 검증했습니다. **인증 API와 실제 Nexus 파일 다운로드는 API 키가 없어 미검증**입니다. 버전·의존성·해시가 확인되지 않으면 미확인 상태를 유지합니다.

- 17 Modrinth: 실제 API에서 Sodium 검색·상세·Minecraft 1.21.1/Fabric 버전 22개·SHA-512/SHA-1 파일 식별·다운로드 URL 재조회와 Iris 의존성 선언을 검증했습니다. 게임 버전/로더는 API 필터와 응답 모델에 보존합니다. 모드팩 등 다른 프로젝트 유형과 다른 프로젝트의 파일은 거부합니다. 파일명만 있는 의존성은 해결된 것으로 간주하지 않고 미해결 선언으로 보존합니다. 실제 파일 저장/캐시는 23~24 단계에서 검증합니다.
