# ModMesh 서버 운영

Python 3.12와 PostgreSQL 17을 기준으로 합니다. 서버는 Manifest·계정·공유 코드 메타데이터를 보관합니다. 게임과 외부 모드 파일은 클라이언트가 원래 출처에서 받습니다.

## 독립 실행

저장소에서 가상환경을 만들고 다음 명령으로 서버 의존성을 설치합니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock '.[server]'
export MODMESH_ENV=production
export MODMESH_DATABASE_URL='postgresql+psycopg://modmesh@127.0.0.1:5432/modmesh'
export MODMESH_DATABASE_PASSWORD_FILE=/secure/modmesh/database-password
.venv/bin/modmesh-server --migrate
.venv/bin/modmesh-server --check
.venv/bin/modmesh-server
```

PostgreSQL에 전용 사용자와 DB를 먼저 만들고 비밀번호 파일을 준비합니다. 이 파일은 소유 계정만 읽도록 보호하고 저장소에 넣지 않습니다. Windows에서는 `.venv\Scripts\python`과 `.venv\Scripts\modmesh-server`, PowerShell 환경 변수 문법을 사용합니다. 서비스 관리자의 실행 계정·작업 디렉터리·환경 변수로 같은 명령을 등록하면 재부팅 후에도 운영할 수 있습니다. 기본 바인드는 loopback의 8731번 포트입니다.

## Docker Compose

Docker Engine과 Compose v2가 필요합니다. 저장소 밖의 비공개 디렉터리에 비밀번호 파일을 준비합니다. 파일은 DB·앱 컨테이너 모두 읽을 수 있어야 합니다. Linux에서는 상위 디렉터리를 0700으로 보호하고 해당 파일만 0444로 두면 호스트의 다른 계정은 상위 디렉터리를 통과할 수 없고 Docker secret 마운트는 읽을 수 있습니다.

```sh
export MODMESH_DB_PASSWORD_FILE=/secure/modmesh/database-password
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail=100 server
```

DB 준비 확인 후 일회성 마이그레이션이 성공해야 서버가 시작합니다. DB 데이터는 `database` 볼륨에 남습니다. 서버 이미지는 일반 사용자로 실행하고 파일시스템은 읽기 전용입니다. 호스트에는 `127.0.0.1:8731`만 노출합니다. `MODMESH_SERVER_PORT`로 호스트 포트를 변경할 수 있습니다. Compose 종료는 `docker compose down`이며 데이터 보존을 위해 운영 서버에서 `down -v`를 사용하지 않습니다.

업데이트 전 백업을 만든 뒤 다음 순서로 실행합니다.

```sh
docker compose build
docker compose stop server
docker compose run --rm migrate
docker compose up -d --wait
```

마이그레이션 실패 시 서버를 열지 말고 원인을 확인합니다. 마이그레이션은 데이터를 보존하는 전진 방식이며 과거 이미지로만 교체해서 DB를 되돌리지 않습니다.

## HTTPS와 메일

외부 공개는 HTTPS 역방향 프록시를 8731번 포트 앞에 둡니다. DB 포트는 외부에 공개하지 않습니다. 클라이언트의 계정 화면에는 HTTPS 서버 URL을 입력합니다. 프록시 헤더가 필요하면 `MODMESH_TRUSTED_PROXY_IPS`에 신뢰하는 프록시의 실제 IP만 지정합니다. 독립 실행에서는 Uvicorn의 `FORWARDED_ALLOW_IPS`를 사용합니다. 임의 클라이언트를 신뢰하는 `*` 설정을 사용하지 않습니다.

메일은 `MODMESH_SMTP_HOST`, `MODMESH_SMTP_PORT`, `MODMESH_SMTP_FROM`, `MODMESH_SMTP_USERNAME`, `MODMESH_SMTP_PASSWORD`로 설정합니다. 기본은 STARTTLS입니다. 독립 실행에서 비밀번호를 파일로 전달하려면 `MODMESH_SMTP_PASSWORD_FILE`을 사용합니다. 환경 변수와 해당 `_FILE`을 동시에 지정하면 설정 오류가 됩니다. 메일이 없으면 가입·코드 발급이 거부됩니다. 공개 운영 전 실제 메일 도착과 확인·복구 흐름을 확인합니다.

## 백업과 복원

계정·버전·공유 코드를 모두 보존하려면 PostgreSQL 전체 DB를 백업합니다. 백업 파일에는 개인정보와 인증 해시가 있으므로 접근을 제한합니다.

```sh
docker compose exec -T db pg_dump -U modmesh -d modmesh -Fc > modmesh.backup
```

복원은 서버를 중단하고 백업과 호환되는 별도 빈 DB에서 먼저 확인합니다. 기존 DB를 지우는 명령은 제공하지 않습니다. 대상 DB 이름을 확인한 다음 다음과 같이 빈 DB로 복원합니다.

```sh
docker compose exec -T db pg_restore -U modmesh -d modmesh_restored --no-owner < modmesh.backup
```

복구한 DB를 선택하고 `--check`와 로그인·팩 조회를 확인한 뒤 서버를 다시 엽니다. 파일 하나의 백업 성공만으로 복구가 보장되지 않으므로 정기적으로 별도 환경에서 복원을 확인합니다.
