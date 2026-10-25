from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.accounts import AccountPanel
from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.core.pack_io import read_manifest
from modmesh.core.packs import PackManifest
from modmesh.remote.packs import PublishingClient


class PublishingPanel(QWidget):
    def __init__(self, accounts: AccountPanel, start: WorkStarter) -> None:
        super().__init__()
        self.accounts, self.start = accounts, start
        self.generation = 0
        self.records: list[dict[str, object]] = []
        self.revisions: list[dict[str, object]] = []
        self.next_offset: int | None = None
        self.version_offset: int | None = None
        self.controls: list[QWidget] = []
        layout = QVBoxLayout(self)
        title = QLabel("온라인 팩")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel("선택한 서버에 Manifest만 게시합니다. 공개 범위를 확인한 뒤 게시하세요.")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.button(row, "내 팩 조회", self.refresh)
        self.button(row, "다음 팩 페이지", self.next_page)
        row.addStretch()
        layout.addLayout(row)
        split = QSplitter()
        self.list = table(("이름", "공개 범위", "최신 개정", "상태"))
        self.list.itemSelectionChanged.connect(self.selected)
        split.addWidget(self.list)
        self.version_list = table(("개정", "SHA-256", "생성 시각"))
        split.addWidget(self.version_list)
        layout.addWidget(split, 1)
        row = QHBoxLayout()
        self.button(row, "다음 버전 페이지", self.next_versions)
        self.button(row, "선택 버전 미리보기", self.preview_version)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("게시할 로컬 .modmesh.json")
        row.addWidget(self.path, 1)
        self.controls.append(self.path)
        self.button(row, "Manifest 선택", self.choose)
        self.button(row, "신규 팩 게시", self.publish)
        self.button(row, "선택 팩에 새 개정 게시", lambda: self.publish(True))
        layout.addLayout(row)
        row = QHBoxLayout()
        self.visibility = QComboBox()
        for label, value in (("비공개", "private"), ("공개", "public"), ("공유 코드", "unlisted")):
            self.visibility.addItem(label, value)
        row.addWidget(self.visibility)
        self.description = QLineEdit()
        self.description.setPlaceholderText("팩 설명")
        self.description.setMaxLength(2000)
        row.addWidget(self.description, 1)
        self.archived = QCheckBox("게시 중단")
        row.addWidget(self.archived)
        self.button(row, "공개 범위·설명 저장", self.edit)
        self.controls.extend([self.visibility, self.description, self.archived])
        layout.addLayout(row)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.share_hours = QSpinBox()
        self.share_hours.setRange(1, 720)
        self.share_hours.setValue(168)
        self.share_hours.setSuffix("시간 후 만료")
        self.share_uses = QSpinBox()
        self.share_uses.setRange(0, 1000000)
        self.share_uses.setSpecialValueText("조회 횟수 무제한")
        self.share_uses.setSuffix("회 조회")
        self.share_revision = QSpinBox()
        self.share_revision.setRange(0, 2**31 - 1)
        self.share_revision.setSpecialValueText("항상 최신 개정")
        for control in (self.share_hours, self.share_uses, self.share_revision):
            row.addWidget(control)
            self.controls.append(control)
        self.button(row, "선택 팩 공유 코드 발급", self.issue_share)
        layout.addLayout(row)
        self.share_code = QLineEdit()
        self.share_code.setReadOnly(True)
        self.share_code.setPlaceholderText(
            "새 코드는 발급 직후에만 표시됩니다. 복사해서 보관하세요."
        )
        layout.addWidget(self.share_code)
        row = QHBoxLayout()
        self.share_list = QComboBox()
        row.addWidget(self.share_list, 1)
        self.controls.append(self.share_list)
        self.share_offset: int | None = None
        self.button(row, "코드 목록", self.load_shares)
        self.button(row, "다음 코드 페이지", self.next_shares)
        self.button(row, "선택 코드 회수", self.revoke_share)
        layout.addLayout(row)
        self.status = QLabel("계정 화면에서 서버를 선택하고 로그인하세요.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.accounts.state_changed.connect(self.context_changed)

    def button(self, row: QHBoxLayout, label: str, callback: Callable[[], None]) -> QPushButton:
        button = QPushButton(label)
        button.clicked.connect(callback)
        row.addWidget(button)
        self.controls.append(button)
        return button

    @property
    def selected_pack(self) -> dict[str, object] | None:
        index = self.list.currentRow()
        return self.records[index] if 0 <= index < len(self.records) else None

    def context_changed(self, label: str) -> None:
        self.generation += 1
        self.records, self.revisions = [], []
        self.list.setRowCount(0)
        self.version_list.setRowCount(0)
        self.preview.clear()
        self.share_code.clear()
        self.share_list.clear()
        self.status.setText(label)
        self.list.setEnabled(True)
        self.version_list.setEnabled(True)
        for control in self.controls:
            control.setEnabled(True)

    def run(
        self, work: Callable[[PublishingClient], object], done: Callable[[object], None]
    ) -> None:
        if self.accounts.client is None or self.accounts.account is None:
            self.status.setText("먼저 계정 화면에서 로그인하세요.")
            return
        client, generation = PublishingClient(self.accounts.client), self.generation
        for control in self.controls:
            control.setEnabled(False)
        self.list.setEnabled(False)
        self.version_list.setEnabled(False)
        self.status.setText("서버에 요청 중…")

        def enable() -> None:
            for control in self.controls:
                control.setEnabled(True)
            self.list.setEnabled(True)
            self.version_list.setEnabled(True)

        def completed(value: object) -> None:
            if generation == self.generation:
                enable()
                self.status.setText("완료")
                done(value)

        def failed(message: str) -> None:
            if generation == self.generation:
                enable()
                self.status.setText(message)

        self.start(lambda cancelled: work(client), completed, failed, name="온라인 팩 요청")

    def refresh(self) -> None:
        self.load_page(0)

    def next_page(self) -> None:
        if self.next_offset is not None:
            self.load_page(self.next_offset)

    def load_page(self, offset: int) -> None:
        def loaded(value: object) -> None:
            assert isinstance(value, dict)
            self.list.setRowCount(0)
            raw = value.get("packs")
            self.records = (
                [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            )
            self.list.setRowCount(len(self.records))
            following = value.get("next_offset")
            self.next_offset = following if type(following) is int else None
            for row, item in enumerate(self.records):
                fill_row(
                    self.list,
                    row,
                    tuple(
                        str(item.get(key, ""))
                        for key in ("name", "visibility", "latest_revision", "archived")
                    ),
                )
            self.status.setText(f"내 팩 {len(self.records)}개 · 선택하면 버전을 조회합니다.")

        self.run(lambda client: client.mine(offset), loaded)

    def selected(self) -> None:
        self.share_code.clear()
        self.share_list.clear()
        if pack := self.selected_pack:
            self.description.setText(str(pack["description"]))
            self.visibility.setCurrentIndex(self.visibility.findData(pack["visibility"]))
            self.archived.setChecked(pack["archived"] is True)
            self.load_versions(0)

    def issue_share(self) -> None:
        if pack := self.selected_pack:
            revision = self.share_revision.value() or None
            seconds, maximum = self.share_hours.value() * 3600, self.share_uses.value() or None

            def issued(value: object) -> None:
                assert isinstance(value, dict)
                self.share_code.setText(str(value["code"]))
                self.load_shares()

            self.run(
                lambda client: client.issue_share(str(pack["id"]), revision, seconds, maximum),
                issued,
            )

    def next_shares(self) -> None:
        if self.share_offset is not None:
            self.load_shares(self.share_offset)

    def load_shares(self, offset: int = 0) -> None:
        if pack := self.selected_pack:

            def loaded(value: object) -> None:
                assert isinstance(value, dict)
                self.share_list.clear()
                following = value.get("next_offset")
                self.share_offset = following if type(following) is int else None
                raw = value.get("shares")
                for item in raw if isinstance(raw, list) else []:
                    if isinstance(item, dict):
                        label = (
                            f"{item['id']} · {item['uses']}/{item['max_uses'] or '∞'}"
                            f" · 만료 {item['expires_at']} · 회수 {item['revoked']}"
                        )
                        self.share_list.addItem(label, item["id"])

            self.run(lambda client: client.shares(str(pack["id"]), offset), loaded)

    def revoke_share(self) -> None:
        if (pack := self.selected_pack) and (identity := self.share_list.currentData()):
            self.run(
                lambda client: client.revoke_share(str(pack["id"]), str(identity)),
                lambda _: self.load_shares(),
            )

    def load_versions(self, offset: int) -> None:
        pack = self.selected_pack
        if pack is None:
            return

        def loaded(value: object) -> None:
            assert isinstance(value, dict)
            self.version_list.setRowCount(0)
            raw = value.get("versions")
            self.revisions = (
                [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            )
            self.version_list.setRowCount(len(self.revisions))
            following = value.get("next_offset")
            self.version_offset = following if type(following) is int else None
            for row, item in enumerate(self.revisions):
                fill_row(
                    self.version_list,
                    row,
                    tuple(str(item.get(key, "")) for key in ("revision", "digest", "created_at")),
                )

        self.run(lambda client: client.versions(str(pack["id"]), offset), loaded)

    def next_versions(self) -> None:
        if self.version_offset is not None:
            self.load_versions(self.version_offset)

    def preview_version(self) -> None:
        pack, index = self.selected_pack, self.version_list.currentRow()
        if pack is None or not 0 <= index < len(self.revisions):
            return
        revision = int(str(self.revisions[index]["revision"]))

        def loaded(value: object) -> None:
            assert isinstance(value, PackManifest)
            self.preview.setPlainText(json.dumps(value.document(), ensure_ascii=False, indent=2))

        self.run(lambda client: client.version(str(pack["id"]), revision), loaded)

    def choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "게시할 Manifest", "", "ModMesh (*.json)")
        if path:
            self.path.setText(path)

    def publish(self, append: bool = False) -> None:
        path, pack = Path(self.path.text()), self.selected_pack
        visibility, description = str(self.visibility.currentData()), self.description.text()
        if append and pack is None:
            return

        def work(client: PublishingClient) -> dict[str, object]:
            manifest = read_manifest(path)
            if append and pack:
                if manifest.pack_id != pack["id"]:
                    raise ValueError("Manifest ID does not match the selected pack")
                return client.append(manifest, int(str(pack["latest_revision"])))
            return client.publish(manifest, visibility, description)

        self.run(work, lambda _: self.refresh())

    def edit(self) -> None:
        if pack := self.selected_pack:
            visibility, description = str(self.visibility.currentData()), self.description.text()
            archived = self.archived.isChecked()
            self.run(
                lambda client: client.edit(str(pack["id"]), visibility, description, archived),
                lambda _: self.refresh(),
            )
