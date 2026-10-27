import json
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
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

from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.rules import RuleRegistration, RuleRegistrationService, RuleReview
from modmesh.core.capabilities import GameCapability
from modmesh.core.config import ClientSettings
from modmesh.core.game_rules import (
    DetectionRule,
    GameRule,
    InstallStrategyKind,
    InstallStrategyRule,
    LaunchKind,
    LaunchRule,
    LoaderKind,
    LoaderRule,
    ModRootRule,
    StoreIds,
    VersionRule,
)
from modmesh.core.games import DiscoveryContext
from modmesh.core.rule_io import read_rule
from modmesh.domain.models import Platform


class RulesPanel(QWidget):
    def __init__(
        self,
        settings: ClientSettings,
        context: DiscoveryContext,
        start: WorkStarter,
        registered: Callable[[RuleRegistration], None],
    ) -> None:
        super().__init__()
        self.settings, self.context, self.start = settings, context, start
        self.registered = registered
        self.generation = 0
        self.reviewed: RuleReview | None = None
        self.reviewed_text = ""
        layout = QVBoxLayout(self)
        title = QLabel("게임 규칙")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel(
            "게임·모드 경로를 정하고 JSON을 생성하거나 기존 규칙을 불러오세요. "
            "검사는 게임 파일을 변경하지 않습니다. 등록은 검토한 규칙과 경로만 저장합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("게임 폴더 · 선택하면 규칙과 실제 파일을 대조합니다")
        self.folder.textChanged.connect(self.invalidate)
        row.addWidget(self.folder, 1)
        choose = QPushButton("게임 폴더…")
        choose.clicked.connect(self.choose_folder)
        row.addWidget(choose)
        layout.addLayout(row)
        split = QSplitter()
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        self.game_id = QLineEdit()
        self.game_id.setPlaceholderText("소문자 ID")
        self.game_id.setMaxLength(64)
        self.game_name = QLineEdit()
        self.game_name.setMaxLength(100)
        self.revision = QSpinBox()
        self.revision.setRange(1, 1000000)
        self.platform = QComboBox()
        for platform_value in Platform:
            self.platform.addItem(platform_value.value, platform_value.value)
        self.platform.setCurrentIndex(max(0, self.platform.findData(context.platform.value)))
        self.executable = QLineEdit()
        self.executable.setPlaceholderText("게임 내 실행 파일 상대 경로")
        self.mod_root = QLineEdit("Mods")
        self.extensions = QLineEdit(".pak")
        self.loader = QComboBox()
        for loader_value in (LoaderKind.NONE, LoaderKind.BEPINEX, LoaderKind.UE4SS):
            self.loader.addItem(loader_value.value, loader_value.value)
        self.marker = QLineEdit()
        self.marker.setPlaceholderText("로더 DLL 상대 경로 · 쉼표로 구분")
        self.placement = QComboBox()
        for placement_value in (
            InstallStrategyKind.FILE,
            InstallStrategyKind.ARCHIVE,
            InstallStrategyKind.PLUGIN,
            InstallStrategyKind.READONLY,
        ):
            self.placement.addItem(placement_value.value, placement_value.value)
        self.steam_id = QLineEdit()
        self.steam_id.setPlaceholderText("선택 사항 · 숫자 App ID")
        self.steam_id.setMaxLength(10)
        self.launch = QComboBox()
        for launch_value in LaunchKind:
            self.launch.addItem(launch_value.value, launch_value.value)
        for label, widget in (
            ("게임 ID", self.game_id),
            ("표시 이름", self.game_name),
            ("규칙 개정", self.revision),
            ("플랫폼", self.platform),
            ("필수 실행 파일", self.executable),
            ("모드 폴더", self.mod_root),
            ("허용 확장자", self.extensions),
            ("로더", self.loader),
            ("로더 표식", self.marker),
            ("배치 방식", self.placement),
            ("Steam App ID", self.steam_id),
            ("실행 정의", self.launch),
        ):
            form.addRow(label, widget)
        self.loader.currentIndexChanged.connect(self.loader_changed)
        generate = QPushButton("폼에서 JSON 생성")
        generate.clicked.connect(self.generate)
        form.addRow(generate)
        split.addWidget(form_widget)
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlaceholderText("검증할 JSON 규칙 · 경로 매핑은 여기에서 편집합니다")
        self.editor.textChanged.connect(self.invalidate)
        editor_layout.addWidget(self.editor, 3)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        editor_layout.addWidget(self.preview, 2)
        split.addWidget(editor_widget)
        split.setSizes([360, 520])
        layout.addWidget(split, 1)
        row = QHBoxLayout()
        load = QPushButton("규칙 JSON 불러오기…")
        load.clicked.connect(self.load)
        row.addWidget(load)
        self.check_button = QPushButton("변경 없이 검사")
        self.check_button.clicked.connect(self.check)
        row.addWidget(self.check_button)
        self.approve = QPushButton("검토한 규칙 승인·등록")
        self.approve.setEnabled(False)
        self.approve.clicked.connect(self.register)
        row.addWidget(self.approve)
        self.workshop = QPushButton("공식 Workshop")
        self.workshop.setEnabled(False)
        self.workshop.clicked.connect(self.open_workshop)
        row.addWidget(self.workshop)
        layout.addLayout(row)
        self.status = QLabel(
            "실행 정의는 저장만 합니다. 게임 실행과 실제 모드 로딩은 별도 검증이 필요합니다."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def invalidate(self, *_args: object) -> None:
        self.generation += 1
        self.reviewed = None
        self.approve.setEnabled(False)
        self.workshop.setEnabled(False)
        self.preview.clear()

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "게임 설치 폴더")
        if folder:
            self.folder.setText(folder)

    def loader_changed(self) -> None:
        kind = LoaderKind(str(self.loader.currentData()))
        if kind == LoaderKind.BEPINEX:
            self.mod_root.setText("BepInEx/plugins")
            self.marker.setText("BepInEx/core/BepInEx.dll,BepInEx/core/BepInEx.Core.dll")
            self.extensions.setText(".dll,.json,.cfg")
        elif kind == LoaderKind.UE4SS:
            self.mod_root.setText("ue4ss/Mods")
            self.marker.setText("ue4ss/UE4SS.dll")
            self.extensions.setText(".lua,.dll,.txt,.json,.ini")
        else:
            self.marker.clear()
        self.placement.setCurrentIndex(
            self.placement.findData("file" if kind == LoaderKind.NONE else "plugin")
        )

    def generate(self) -> None:
        kind = InstallStrategyKind(str(self.placement.currentData()))
        loader = LoaderKind(str(self.loader.currentData()))
        launch = LaunchKind(str(self.launch.currentData()))
        app_id = self.steam_id.text().strip() or None
        extensions = tuple(
            part.strip() for part in self.extensions.text().split(",") if part.strip()
        )
        caps = [GameCapability.DISCOVERY, GameCapability.VERSION, GameCapability.MOD_DISCOVERY]
        if kind != InstallStrategyKind.READONLY:
            caps += [
                GameCapability.INSTALL_RULES,
                GameCapability.REMOVAL_RULES,
                GameCapability.PROFILES,
            ]
        elif app_id:
            caps.append(GameCapability.WORKSHOP_HANDOFF)
        rule = GameRule(
            1,
            self.game_id.text().strip(),
            self.game_name.text().strip(),
            self.revision.value(),
            "custom",
            (Platform(str(self.platform.currentData())),),
            StoreIds(app_id),
            DetectionRule((self.executable.text().strip(),)),
            VersionRule("steam-build" if app_id else "unknown"),
            LoaderRule(
                loader,
                tuple(part.strip() for part in self.marker.text().split(",") if part.strip()),
            ),
            (
                ModRootRule(
                    "mods",
                    self.mod_root.text().strip(),
                    "files" if loader == LoaderKind.NONE else loader.value,
                ),
            ),
            (InstallStrategyRule("user-mods", kind, "mods", extensions),),
            tuple(extension.lstrip(".") for extension in extensions),
            "none" if kind == InstallStrategyKind.READONLY else "managed-files",
            LaunchRule(
                launch,
                self.executable.text().strip() if launch == LaunchKind.EXECUTABLE else None,
                app_id if launch == LaunchKind.STEAM else None,
            ),
            tuple(caps),
        )
        self.editor.setPlainText(
            json.dumps(json.loads(rule.to_json()), ensure_ascii=False, indent=2)
        )

    def load(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "게임 규칙", filter="JSON (*.json)")
        if not selected:
            return

        def loaded(value: object) -> None:
            assert isinstance(value, GameRule)
            self.editor.setPlainText(
                json.dumps(json.loads(value.to_json()), ensure_ascii=False, indent=2)
            )

        self.start(
            lambda _cancel: read_rule(Path(selected)), loaded, self.failed, name="게임 규칙 읽기"
        )

    def check(self) -> None:
        text, folder = self.editor.toPlainText(), self.folder.text().strip()
        self.invalidate()
        generation = self.generation
        service = RuleRegistrationService(self.settings.paths.database, self.context)

        def reviewed(value: object) -> None:
            if generation != self.generation:
                return
            assert isinstance(value, RuleReview)
            self.reviewed, self.reviewed_text = value, text
            self.preview.setPlainText(
                json.dumps(
                    {
                        "검사만 수행": True,
                        "게임": value.rule.name,
                        "이전 규칙": value.previous_digest,
                        "승인할 SHA-256": value.rule.digest,
                        "모드 경로": [asdict(root) for root in value.rule.mod_roots],
                        "기능": [
                            {
                                "기능": support.capability.value,
                                "상태": support.state.value,
                                "안내": support.message,
                            }
                            for support in value.support
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self.approve.setEnabled(True)
            self.workshop.setEnabled(GameCapability.WORKSHOP_HANDOFF in value.rule.capabilities)
            self.status.setText(
                "검사 완료. 등록하려면 검토한 규칙을 승인하세요. 게임 파일은 변경하지 않았습니다."
            )

        self.status.setText("규칙과 선택한 게임 경로를 확인하고 있습니다…")
        self.start(
            lambda _cancel: service.review(text, path=Path(folder) if folder else None),
            reviewed,
            self.failed,
            name="게임 규칙 검사",
        )

    def register(self) -> None:
        review = self.reviewed
        if review is None or self.editor.toPlainText() != self.reviewed_text:
            return
        text, folder = self.reviewed_text, self.folder.text().strip()
        self.approve.setEnabled(False)
        service = RuleRegistrationService(self.settings.paths.database, self.context)

        def work(cancelled: threading.Event) -> RuleRegistration:
            return service.register(
                text,
                review.rule.digest,
                expected_digest=review.previous_digest,
                path=Path(folder) if folder else None,
                cancelled=cancelled.is_set,
            )

        def registered(value: object) -> None:
            assert isinstance(value, RuleRegistration)
            self.invalidate()
            self.registered(value)
            self.status.setText(
                "규칙을 등록했습니다. 내 게임에서 선택하세요. "
                "기존 게임 파일과 데이터는 보존했습니다."
            )

        self.start(work, registered, self.failed, name="게임 규칙 등록")

    def open_workshop(self) -> None:
        if self.reviewed is not None and self.reviewed.rule.store_ids.steam is not None:
            QDesktopServices.openUrl(
                QUrl(
                    "https://steamcommunity.com/workshop/browse/?appid="
                    + self.reviewed.rule.store_ids.steam
                )
            )

    def failed(self, message: str) -> None:
        self.status.setText("규칙을 확인할 수 없습니다. " + message)
