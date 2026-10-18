# ModMesh

여러 게임과 배포처의 모드를 검색·설치·관리하고 재현 가능한 모드팩으로 공유하기 위한 데스크톱 프로그램입니다.

## 개발 상태

01번: 저장소를 초기화했습니다. 최신 Python 개발계획서의 순서대로 기능을 구현하고 검증합니다.

## 기술 스택

Python 3.12, PySide6 / Qt Widgets, httpx, SQLite, FastAPI / PostgreSQL.

지원 목표는 Palworld Steam, Minecraft Java Edition, ARK: Survival Evolved Steam입니다. 게임별 지원은 해당 어댑터를 구현하고 실제로 검증한 뒤 표시합니다.
