# Windows 배포판 0.1.0rc1

Windows 10 2004 이상 / Windows 11 x64용 릴리스 후보입니다. Python 설치가 필요하지 않습니다. 실게임 모드 로딩과 일부 외부 서비스 검증은 남아 있습니다.

## 다운로드와 실행

저장소의 Actions → Quality → 성공한 실행 → Artifacts에서 `ModMesh-windows-x64`를 받습니다. GitHub 로그인이 필요할 수 있습니다. 압축을 풀면 포터블 ZIP, 설치 EXE, 제삼자 소스 ZIP, `SHA256SUMS.txt`와 패키지 검증 보고서가 있습니다.

- 설치형: `ModMesh-0.1.0rc1-windows-x64-setup.exe`를 실행합니다. 현재 사용자에게 설치하며 관리자 권한이 필요하지 않습니다. 시작 메뉴에서 ModMesh를 실행합니다.
- 포터블: `ModMesh-0.1.0rc1-windows-x64-portable.zip` 전체를 압축 해제하고 `ModMesh/ModMesh.exe`를 실행합니다. `_internal`과 `licenses`도 함께 보관합니다. 포터블도 사용자 설정과 데이터는 아래 공통 경로를 사용합니다.
- 이 후보판은 코드 서명하지 않았습니다. Windows가 게시자를 확인하지 못할 수 있습니다. 저장소에서 받은 파일인지 확인하고 `Get-FileHash <파일> -Algorithm SHA256` 결과를 동봉된 체크섬과 비교할 수 있습니다.

로컬 기능에는 계정과 서버가 필요하지 않습니다. 첫 실행에서 게임을 탐색하며 필요한 경우 게임 폴더를 수동 등록합니다. 모드를 설치·제거하거나 프로필을 전환할 때는 화면의 변경 계획을 검토하고 승인합니다.

## 설정과 업데이트

- 기본 설치 위치: `%LOCALAPPDATA%/Programs/ModMesh/versions/0.1.0rc1`
- 설정: `%APPDATA%/ModMesh/config.toml`
- 데이터·DB·로그: `%LOCALAPPDATA%/ModMesh`
- 캐시: `%LOCALAPPDATA%/ModMesh/cache`

설정 파일이 없어도 실행됩니다. 서버 주소와 출처 API 키 설정은 저장소 README를 참고하세요. 로그인 유지에는 Windows 보안 저장소를 사용합니다.

업데이트 전 ModMesh를 종료하고 새 설치 프로그램을 실행합니다. 설치형은 버전별 폴더를 사용하며 새 버전을 시작 메뉴에 연결합니다. 포터블은 새 폴더에 전체 압축 해제합니다. 기존 사용자 데이터는 공유합니다. 이전 버전으로 데이터 호환성이 보장되는 것은 아니므로 중요한 데이터는 먼저 백업하세요. 자동 업데이트는 제공하지 않습니다.

설치형 제거는 Windows 설치된 앱의 ModMesh 또는 설치 폴더의 `unins000.exe`를 사용합니다. 제거 프로그램은 설치한 프로그램 파일을 제거하며 사용자 설정·DB·캐시·게임·세이브와 설치 폴더의 미등록 파일은 보존합니다. 이미 설치한 게임 모드도 제거하지 않습니다. 포터블은 프로그램 폴더를 삭제하면 됩니다. 서버는 별도 Python/Docker 배포입니다.

## 검증 범위

CI는 Windows에서 실제 포터블 EXE와 설치한 EXE의 Qt 화면·게임 탐색을 실행합니다. Python 검색 경로를 제거한 환경에서 재실행, 같은 버전 재설치와 제거 후 사용자 데이터 보존도 확인합니다. 새 버전 간 데이터 마이그레이션, 일반 사용자 PC·보안 제품별 동작과 실제 게임 로딩을 모두 검증한 것은 아닙니다.

Palworld와 Minecraft는 탐지·계획·설치·제거·프로필 기능을 제공합니다. 게임별 실제 모드 로딩은 미검증입니다. ARK: Survival Evolved Steam판은 로컬 상태 조회와 공식 Workshop 연결만 제공합니다. Modrinth 실다운로드는 검증했으며 Nexus 인증 다운로드, CurseForge 승인 계정 API, 외부 SMTP 배달과 실계정 Workshop 구독은 별도 검증이 필요합니다. 상세 제한은 저장소 README에 있습니다.

## 라이선스와 의존성

도움말 → 정보 / 라이선스에서 포함된 제삼자 고지를 확인할 수 있습니다. `THIRD_PARTY_NOTICES.txt`, `runtime-packages.json`, `licenses/`를 배포판과 함께 보관하세요. Qt/PySide6/shiboken6는 LGPL-3.0-only 조건으로 동적 연결합니다. `_internal/PySide6`와 `_internal/shiboken6`의 라이브러리를 인터페이스 호환 수정본으로 교체할 수 있으며, 그러한 수정의 디버깅을 위한 역공학을 제한하지 않습니다. 해당 라이선스 전문과 하위 의존성 고지는 `licenses/Qt`에 있습니다.

같은 Artifacts의 `ModMesh-0.1.0rc1-third-party-sources.zip`에 사용한 QtBase·QtSvg·QtImageFormats·PySide/shiboken 원본 소스와 URL·SHA-256 목록을 포함합니다. 바이너리를 다시 배포할 때에도 필요한 소스와 고지·라이선스를 함께 제공해야 합니다. 이 문서는 ModMesh 자체에 새로운 오픈소스 라이선스를 부여하지 않습니다.
