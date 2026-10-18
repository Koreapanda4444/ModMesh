# 검증 범위

- 01~12: 패키지 빌드, Ruff, 엄격한 mypy, Qt 이벤트 루프, HTTP health, SQLite 영속성/롤백/동시 접근, 식별·해시·어댑터 계약과 탐지 엔진 직접 실행을 검증했습니다. Windows/Ubuntu CI는 각 푸시에서 별도로 확인합니다.
- 13 Palworld: 임시 Steam 메타데이터에서 여러 라이브러리, App ID, 빌드 ID, 수동 경로, 잘못된 파일/경로 거부를 검증했습니다. **실제 Palworld Steam 설치와 게임 실행은 미검증**입니다. 의미상 게임 버전을 Steam build ID로 추측하지 않습니다.
- Windows 독립 실행 파일/설치 패키지의 배포 검증은 아직 수행하지 않았습니다. 현재 CI는 Python 패키지와 offscreen Qt 실행을 검증합니다.
