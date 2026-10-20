from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.search import WorkStarter
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ModMeshError
from modmesh.remote.accounts import AccountClient
from modmesh.storage.metadata import MetadataStore


class AccountPanel(QWidget):
    state_changed = Signal(str)

    def __init__(self, settings: ClientSettings, store: MetadataStore, start: WorkStarter) -> None:
        super().__init__()
        self.store, self.start = store, start
        self.client: AccountClient | None = None
        self.account: dict[str, object] | None = None
        self.generation = 0
        self.controls: list[QWidget] = []
        layout = QVBoxLayout(self)
        title = QLabel("계정")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("게스트도 로컬 모드 관리·프로필·모드팩을 사용할 수 있습니다."))
        row = QHBoxLayout()
        saved = store.get("desktop", "server") or {}
        self.server = QLineEdit(str(saved.get("url") or settings.server_url or ""))
        self.server.setPlaceholderText("https://서버주소 또는 http://localhost:8765")
        row.addWidget(self.server, 1)
        select = QPushButton("서버 선택")
        select.clicked.connect(self.select_server)
        row.addWidget(select)
        self.controls.extend([self.server, select])
        layout.addLayout(row)
        self.session = QLabel("게스트 · 로컬 모드 관리")
        layout.addWidget(self.session)
        self.email = QLineEdit()
        self.email.setPlaceholderText("이메일 (ModMesh 자체 계정)")
        self.password = QLineEdit()
        self.password.setPlaceholderText("비밀번호 (15–128자)")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.code = QLineEdit()
        self.code.setPlaceholderText("메일로 받은 확인·복구 코드")
        self.code.setEchoMode(QLineEdit.EchoMode.Password)
        self.next_password = QLineEdit()
        self.next_password.setPlaceholderText("새 비밀번호 (비밀번호 변경·복구 시)")
        self.next_password.setEchoMode(QLineEdit.EchoMode.Password)
        for field in (self.email, self.password, self.code, self.next_password):
            layout.addWidget(field)
            self.controls.append(field)
        self.remember = QCheckBox("OS 보안 저장소에 로그인 유지")
        layout.addWidget(self.remember)
        self.controls.append(self.remember)
        row = QHBoxLayout()
        self.action = QComboBox()
        for label, value in (
            ("로그인", "login"),
            ("회원가입", "register"),
            ("확인 메일 재발송", "verification"),
            ("이메일 확인", "verify"),
            ("비밀번호 복구 메일", "recovery"),
            ("복구 코드로 비밀번호 재설정", "reset"),
            ("비밀번호 변경", "password"),
            ("이메일 변경 요청", "email"),
            ("이메일 변경 코드 확인", "email/confirm"),
        ):
            self.action.addItem(label, value)
        row.addWidget(self.action, 1)
        execute = QPushButton("실행")
        execute.clicked.connect(self.execute)
        row.addWidget(execute)
        self.controls.extend([self.action, execute])
        layout.addLayout(row)
        row = QHBoxLayout()
        for label, callback in (
            ("세션 확인", self.refresh),
            ("로그아웃 · 게스트", self.logout),
            ("모든 세션 폐기", lambda: self.logout(True)),
            ("내 팩 새로고침", self.load_packs),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
            self.controls.append(button)
        layout.addLayout(row)
        self.packs = QTextEdit()
        self.packs.setReadOnly(True)
        self.packs.setPlainText("로그인하면 선택한 서버의 내 팩 목록을 확인할 수 있습니다.")
        layout.addWidget(self.packs, 1)
        self.status = QLabel("계정 없이 계속 사용할 수 있습니다.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        if self.server.text():
            QTimer.singleShot(0, self.select_server)

    def set_account(self, value: dict[str, object] | None) -> None:
        self.account = value
        label = str(value.get("email", "로그인")) if value else "게스트 · 로컬 모드 관리"
        self.session.setText(label)
        self.state_changed.emit(label)
        if value is None:
            self.packs.clear()

    def run(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        generation = self.generation
        for control in self.controls:
            control.setEnabled(False)
        self.status.setText("서버에 요청 중…")

        def finish(value: object) -> None:
            if generation == self.generation:
                for control in self.controls:
                    control.setEnabled(True)
                self.status.setText("완료")
                done(value)

        def failed(message: str) -> None:
            if generation == self.generation:
                for control in self.controls:
                    control.setEnabled(True)
                self.status.setText(message)
                if '"code": "authentication"' in message:
                    self.set_account(None)

        self.start(lambda cancelled: work(), finish, failed, name="계정 요청")

    def select_server(self) -> None:
        try:
            client = AccountClient(self.server.text().strip())
        except ModMeshError as exc:
            self.status.setText(exc.message)
            return
        self.generation += 1
        self.client = client
        self.store.put("desktop", "server", {"url": client.server})
        self.set_account(None)
        self.run(client.restore, self.restored)

    def restored(self, value: object) -> None:
        self.set_account(value if isinstance(value, dict) else None)
        if self.account:
            self.load_packs()

    def execute(self) -> None:
        client = self.client
        if client is None:
            self.status.setText("먼저 서버를 선택하세요.")
            return
        action = str(self.action.currentData())
        email, password, code = self.email.text(), self.password.text(), self.code.text()
        next_password, remember = self.next_password.text(), self.remember.isChecked()
        self.password.clear()
        self.next_password.clear()
        self.code.clear()
        if action == "login":
            self.run(lambda: client.login(email, password, remember), self.logged_in)
            return
        body: dict[str, object]
        if action in {"register", "email"}:
            body = {"email": email, "password": password}
        elif action in {"verification", "recovery"}:
            body = {"email": email}
        elif action == "reset":
            body = {"code": code, "password": next_password}
        elif action == "password":
            body = {"current_password": password, "password": next_password}
        else:
            body = {"code": code}

        def completed(value: object) -> None:
            self.status.setText("요청 완료. 메일 요청은 수신함을 확인하세요.")
            if action in {"password", "reset", "email/confirm"}:
                self.set_account(None)

        self.run(lambda: client.auth_action(action, body), completed)

    def logged_in(self, value: object) -> None:
        assert isinstance(value, dict)
        self.set_account(value)
        notice = value.get("storage_notice")
        if notice:
            self.status.setText(str(notice))
        else:
            self.load_packs()

    def refresh(self) -> None:
        if self.client:
            client = self.client
            self.run(lambda: client.request("GET", "/v1/account"), self.restored)

    def logout(self, all_sessions: bool = False) -> None:
        if self.client:
            client = self.client
            self.set_account(None)
            self.run(lambda: client.logout(all_sessions), lambda _: self.set_account(None))

    def load_packs(self) -> None:
        if self.client and self.account:
            client = self.client

            def loaded(value: object) -> None:
                assert isinstance(value, dict)
                items = value.get("packs", [])
                self.packs.setPlainText(
                    "\n".join(str(item.get("name", "")) for item in items if isinstance(item, dict))
                    if isinstance(items, list) and items
                    else "게시한 팩이 없습니다."
                )

            self.run(lambda: client.request("GET", "/v1/account/packs"), loaded)
