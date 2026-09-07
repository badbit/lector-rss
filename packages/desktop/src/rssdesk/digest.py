"""Opciones de la revista sobre la selección actual del lector."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
)
from rsscore.config import MagazineConfig
from rsscore.rules.store import load_rules


class DigestDialog(QDialog):
    def __init__(self, conn, cfg: MagazineConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Preparar revista EPUB")
        self.resize(470, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Se utilizará la carpeta o búsqueda abierta en el lector."))
        form = QFormLayout()
        self.title = QLineEdit(cfg.title)
        form.addRow("Título", self.title)
        self.mode = QComboBox()
        self.mode.addItem("Artículos completos", "full")
        self.mode.addItem("Extractos breves del texto fuente", "excerpt")
        self.mode.setCurrentIndex(1 if cfg.content_mode == "excerpt" else 0)
        form.addRow("Contenido", self.mode)
        self.words = QSpinBox()
        self.words.setRange(30, 1000)
        self.words.setValue(cfg.excerpt_words)
        form.addRow("Palabras por extracto", self.words)
        self.limit = QSpinBox()
        self.limit.setRange(1, 1000)
        self.limit.setValue(cfg.max_articles)
        form.addRow("Máximo de artículos", self.limit)
        layout.addLayout(form)
        layout.addWidget(QLabel("Filtrar por alguna de estas reglas (opcional):"))
        self.rules = QListWidget()
        for rule in load_rules(conn):
            item = QListWidgetItem(rule.name, self.rules)
            item.setData(Qt.ItemDataRole.UserRole, rule.id)
            checked = rule.id in cfg.rules or rule.name in cfg.rules
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        layout.addWidget(self.rules)
        self.send = QCheckBox("Enviar también por correo al Kindle")
        layout.addWidget(self.send)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generar EPUB")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def options(self) -> MagazineConfig:
        return self.cfg.model_copy(update={
            "title": self.title.text().strip() or "Mi revista",
            "content_mode": self.mode.currentData(), "excerpt_words": self.words.value(),
            "max_articles": self.limit.value(),
            "rules": [self.rules.item(i).data(Qt.ItemDataRole.UserRole)
                      for i in range(self.rules.count())
                      if self.rules.item(i).checkState() == Qt.CheckState.Checked],
        })
