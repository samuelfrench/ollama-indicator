#!/usr/bin/env python3
"""Translucent desktop widget showing local Ollama service status and GPU metrics."""

import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from PySide6.QtCore import QPoint, QThread, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

OLLAMA_URL = "http://localhost:11434"
COMFYUI_URL = "http://127.0.0.1:8188"
DYNAMO_TABLE = "clawd-bot-tasks"
DYNAMO_INDEX = "provider-submitted-index"

STATUS_INTERVAL_MS = 10_000      # 10s
MODEL_LIST_INTERVAL_MS = 60_000  # 60s
GPU_INTERVAL_MS = 3_000          # 3s
TASK_INTERVAL_MS = 60_000        # 60s
COMFYUI_INTERVAL_MS = 10_000     # 10s
COUNTDOWN_INTERVAL_MS = 1_000    # 1s


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class OllamaStatus:
    running: bool = False
    version: str = ""
    error: str = ""


@dataclass
class LoadedModel:
    name: str = ""
    size: int = 0           # total model size bytes
    size_vram: int = 0      # VRAM bytes
    parameter_size: str = ""
    quantization: str = ""
    expires_at: str = ""
    context_length: int = 0

    @property
    def size_gb(self) -> float:
        return self.size / (1024 ** 3)

    @property
    def vram_gb(self) -> float:
        return self.size_vram / (1024 ** 3)

    def time_until_expiry(self) -> str:
        if not self.expires_at:
            return ""
        try:
            dt = datetime.fromisoformat(self.expires_at)
            now = datetime.now(timezone.utc)
            delta = dt - now
            secs = int(delta.total_seconds())
            if secs <= 0:
                return "expiring"
            if secs >= 3600:
                return f"{secs // 3600}h {(secs % 3600) // 60}m"
            return f"{secs // 60}m"
        except (ValueError, TypeError):
            return ""


@dataclass
class ModelInfo:
    name: str = ""
    size: int = 0           # disk size bytes
    parameter_size: str = ""
    quantization: str = ""
    family: str = ""

    @property
    def size_gb(self) -> float:
        return self.size / (1024 ** 3)


@dataclass
class GpuMetrics:
    available: bool = False
    utilization: float = 0.0
    mem_used_gb: float = 0.0
    mem_total_gb: float = 0.0
    temperature: int = 0

    @property
    def mem_pct(self) -> float:
        if self.mem_total_gb <= 0:
            return 0.0
        return self.mem_used_gb / self.mem_total_gb * 100


@dataclass
class TaskInfo:
    task_id: str = ""
    project: str = ""
    status: str = ""
    prompt: str = ""
    submitted_at: str = ""
    completed_at: str = ""

    def age_str(self) -> str:
        ts = self.completed_at or self.submitted_at
        if not ts:
            return ""
        try:
            dt = datetime.fromisoformat(ts)
            now = datetime.now(timezone.utc)
            secs = int((now - dt).total_seconds())
            if secs < 0:
                secs = 0
            if secs < 60:
                return f"{secs}s ago"
            if secs < 3600:
                return f"{secs // 60}m ago"
            if secs < 86400:
                return f"{secs // 3600}h ago"
            return f"{secs // 86400}d ago"
        except (ValueError, TypeError):
            return ""


@dataclass
class ComfyUIStatus:
    running: bool = False
    queue_pending: int = 0
    queue_running: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class OllamaStatusWorker(QThread):
    finished = Signal(OllamaStatus, list)  # status, loaded_models

    def run(self):
        status = OllamaStatus()
        loaded: list[LoadedModel] = []
        try:
            r = requests.get(f"{OLLAMA_URL}/api/version", timeout=3)
            r.raise_for_status()
            status.running = True
            status.version = r.json().get("version", "")
        except Exception as e:
            status.error = str(e)
            self.finished.emit(status, loaded)
            return

        try:
            r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
            r.raise_for_status()
            for m in r.json().get("models", []):
                details = m.get("details", {})
                loaded.append(LoadedModel(
                    name=m.get("name", ""),
                    size=m.get("size", 0),
                    size_vram=m.get("size_vram", 0),
                    parameter_size=details.get("parameter_size", ""),
                    quantization=details.get("quantization_level", ""),
                    expires_at=m.get("expires_at", ""),
                    context_length=m.get("context_length", 0),
                ))
        except Exception:
            pass

        self.finished.emit(status, loaded)


class ModelListWorker(QThread):
    finished = Signal(list)  # list[ModelInfo]

    def run(self):
        models: list[ModelInfo] = []
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            r.raise_for_status()
            for m in r.json().get("models", []):
                details = m.get("details", {})
                models.append(ModelInfo(
                    name=m.get("name", ""),
                    size=m.get("size", 0),
                    parameter_size=details.get("parameter_size", ""),
                    quantization=details.get("quantization_level", ""),
                    family=details.get("family", ""),
                ))
        except Exception:
            pass
        self.finished.emit(models)


class GpuWorker(QThread):
    finished = Signal(GpuMetrics)

    def run(self):
        metrics = GpuMetrics()
        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(",")
                if len(parts) >= 4:
                    metrics.available = True
                    metrics.utilization = float(parts[0].strip())
                    metrics.mem_used_gb = float(parts[1].strip()) / 1024
                    metrics.mem_total_gb = float(parts[2].strip()) / 1024
                    metrics.temperature = int(float(parts[3].strip()))
        except Exception:
            pass
        self.finished.emit(metrics)


class TaskWorker(QThread):
    finished = Signal(list)  # list[TaskInfo]

    def run(self):
        tasks: list[TaskInfo] = []
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = ddb.Table(DYNAMO_TABLE)
            resp = table.query(
                IndexName=DYNAMO_INDEX,
                KeyConditionExpression="provider = :p",
                ExpressionAttributeValues={":p": "ollama"},
                ScanIndexForward=False,
                Limit=10,
            )
            for item in resp.get("Items", []):
                tasks.append(TaskInfo(
                    task_id=item.get("task_id", ""),
                    project=item.get("project", ""),
                    status=item.get("status", ""),
                    prompt=item.get("prompt", ""),
                    submitted_at=item.get("submitted_at", ""),
                    completed_at=item.get("completed_at", ""),
                ))
        except Exception:
            pass
        self.finished.emit(tasks)


class ComfyUIWorker(QThread):
    finished = Signal(ComfyUIStatus)

    def run(self):
        status = ComfyUIStatus()
        try:
            r = requests.get(f"{COMFYUI_URL}/queue", timeout=3)
            r.raise_for_status()
            data = r.json()
            status.running = True
            status.queue_pending = len(data.get("queue_pending", []))
            status.queue_running = len(data.get("queue_running", []))
        except Exception:
            pass
        self.finished.emit(status)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _bar_color(pct: float) -> QColor:
    if pct >= 90:
        return QColor(239, 68, 68)    # red
    if pct >= 75:
        return QColor(249, 115, 22)   # orange
    if pct >= 50:
        return QColor(234, 179, 8)    # yellow
    return QColor(34, 197, 94)        # green


def _fmt_size(size_bytes: int) -> str:
    gb = size_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = size_bytes / (1024 ** 2)
    return f"{mb:.0f} MB"


def _add_separator(layout):
    sep = QWidget()
    sep.setFixedHeight(1)
    sep.setStyleSheet("background-color: rgba(100, 100, 120, 80);")
    layout.addWidget(sep)


# ---------------------------------------------------------------------------
# Custom painted widgets
# ---------------------------------------------------------------------------

class StatusRow(QWidget):
    """Shows Ollama running/stopped status and version."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._version = ""
        self.setFixedHeight(22)

    def set_data(self, running: bool, version: str):
        self._running = running
        self._version = version
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        font = QFont("sans-serif", 9)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)

        # Status dot + text
        dot_color = QColor(34, 197, 94) if self._running else QColor(239, 68, 68)
        p.setBrush(dot_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(4, 7, 8, 8)

        text = "Running" if self._running else "Stopped"
        p.setPen(dot_color)
        p.drawText(16, 15, text)

        # Version right-aligned
        if self._version:
            p.setPen(QColor(100, 100, 120))
            ver = f"v{self._version}"
            fm = p.fontMetrics()
            p.drawText(w - fm.horizontalAdvance(ver) - 4, 15, ver)

        p.end()


class ModelCard(QWidget):
    """Shows a loaded model with name, size, VRAM bar, expiry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model: LoadedModel | None = None
        self._gpu_total_gb: float = 24.0
        self.setFixedHeight(48)

    def set_data(self, model: LoadedModel, gpu_total_gb: float = 24.0):
        self._model = model
        self._gpu_total_gb = gpu_total_gb
        self.update()

    def paintEvent(self, event):
        m = self._model
        if m is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        font = QFont("sans-serif", 9)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        fm = p.fontMetrics()

        # Model name in purple
        p.setPen(QColor(139, 92, 246))
        p.drawText(4, 14, m.name)

        # Size + params right-aligned
        info = f"{m.parameter_size}"
        if m.quantization:
            info += f" · {m.quantization}"
        p.setPen(QColor(100, 100, 120))
        p.drawText(w - fm.horizontalAdvance(info) - 4, 14, info)

        # VRAM bar
        bar_y = 22
        bar_h = 10
        bar_radius = 5
        vram_pct = (m.vram_gb / self._gpu_total_gb * 100) if self._gpu_total_gb > 0 else 0

        bg_path = QPainterPath()
        bg_path.addRoundedRect(4, bar_y, w - 8, bar_h, bar_radius, bar_radius)
        p.fillPath(bg_path, QColor(40, 40, 55))

        fill_w = max(bar_h, (w - 8) * vram_pct / 100)
        fill_path = QPainterPath()
        fill_path.addRoundedRect(4, bar_y, fill_w, bar_h, bar_radius, bar_radius)
        p.fillPath(fill_path, _bar_color(vram_pct))

        # VRAM text + expiry below bar
        small_font = QFont("sans-serif", 8)
        p.setFont(small_font)
        sfm = p.fontMetrics()

        vram_text = f"VRAM: {m.vram_gb:.1f} GB ({vram_pct:.0f}%)"
        p.setPen(QColor(160, 160, 180))
        p.drawText(4, 44, vram_text)

        expiry = m.time_until_expiry()
        if expiry:
            exp_text = f"expires: {expiry}"
            p.setPen(QColor(100, 100, 120))
            p.drawText(w - sfm.horizontalAdvance(exp_text) - 4, 44, exp_text)

        p.end()


class GpuRow(QWidget):
    """Shows GPU VRAM bar, utilization, and temperature."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metrics: GpuMetrics | None = None
        self.setFixedHeight(44)

    def set_data(self, metrics: GpuMetrics):
        self._metrics = metrics
        self.update()

    def paintEvent(self, event):
        m = self._metrics
        if m is None or not m.available:
            p = QPainter(self)
            p.setPen(QColor(100, 100, 120))
            font = QFont("sans-serif", 8)
            p.setFont(font)
            p.drawText(4, 14, "No NVIDIA GPU detected")
            p.end()
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        # Label
        label_font = QFont("sans-serif", 8)
        label_font.setWeight(QFont.Weight.Medium)
        p.setFont(label_font)
        fm = p.fontMetrics()

        p.setPen(QColor(100, 100, 120))
        p.drawText(4, 12, "GPU")

        # VRAM text
        vram_text = f"{m.mem_used_gb:.1f} / {m.mem_total_gb:.1f} GB"
        p.setPen(QColor(160, 160, 180))
        p.drawText(30, 12, vram_text)

        # Utilization right
        util_text = f"{m.utilization:.0f}%"
        p.setPen(_bar_color(m.utilization))
        util_w = fm.horizontalAdvance(util_text)

        # Temperature far right
        temp_text = f"{m.temperature}\u00b0C"
        temp_w = fm.horizontalAdvance(temp_text)
        if m.temperature >= 80:
            temp_color = QColor(239, 68, 68)
        elif m.temperature >= 70:
            temp_color = QColor(249, 115, 22)
        else:
            temp_color = QColor(180, 180, 200)
        p.setPen(temp_color)
        p.drawText(w - temp_w - 4, 12, temp_text)

        p.setPen(_bar_color(m.utilization))
        p.drawText(w - temp_w - util_w - 16, 12, util_text)

        # VRAM bar
        bar_y = 20
        bar_h = 10
        bar_radius = 5
        pct = m.mem_pct

        bg_path = QPainterPath()
        bg_path.addRoundedRect(4, bar_y, w - 8, bar_h, bar_radius, bar_radius)
        p.fillPath(bg_path, QColor(40, 40, 55))

        fill_w = max(bar_h, (w - 8) * pct / 100)
        fill_path = QPainterPath()
        fill_path.addRoundedRect(4, bar_y, fill_w, bar_h, bar_radius, bar_radius)
        p.fillPath(fill_path, _bar_color(pct))

        p.end()


class TasksSection(QWidget):
    """Collapsible section showing clawd-bot Ollama tasks with prompt text."""

    _COLLAPSED_H = 22
    _ROW_H = 36  # taller rows to fit prompt text

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = False
        self._tasks: list[TaskInfo] = []
        self.setFixedHeight(self._COLLAPSED_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _content_height(self) -> int:
        return self._COLLAPSED_H + self._ROW_H * min(len(self._tasks), 10)

    def set_data(self, tasks: list[TaskInfo]):
        self._tasks = tasks
        if self._expanded:
            self.setFixedHeight(self._content_height())
        self.update()

    def mousePressEvent(self, event):
        self._expanded = not self._expanded
        if self._expanded and self._tasks:
            self.setFixedHeight(self._content_height())
        else:
            self.setFixedHeight(self._COLLAPSED_H)
        parent = self.parent()
        while parent:
            if hasattr(parent, "adjustSize"):
                parent.adjustSize()
                break
            parent = parent.parent() if hasattr(parent, "parent") else None
        event.accept()

    def _truncate_prompt(self, prompt: str, fm, max_w: int) -> str:
        """Truncate prompt to fit within max_w pixels."""
        if not prompt:
            return ""
        # Strip common autonomous preamble
        for prefix in ("You are an autonomous agent working toward this goal: ",):
            if prompt.startswith(prefix):
                prompt = prompt[len(prefix):]
                break
        # Single line, truncate to pixel width
        prompt = prompt.replace("\n", " ").strip()
        if fm.horizontalAdvance(prompt) <= max_w:
            return prompt
        while prompt and fm.horizontalAdvance(prompt + "\u2026") > max_w:
            prompt = prompt[:-1]
        return prompt.rstrip() + "\u2026"

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        font = QFont("sans-serif", 8)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        fm = p.fontMetrics()

        y = 14
        arrow = "\u25be" if self._expanded else "\u25b8"
        count = len(self._tasks)
        header = f"CLAWD-BOT TASKS {arrow}  ({count} recent)"
        p.setPen(QColor(100, 100, 120))
        p.drawText(4, y, header)

        if not self._expanded or not self._tasks:
            p.end()
            return

        STATUS_COLORS = {
            "pending": QColor(100, 100, 120),
            "running": QColor(59, 130, 246),
            "completed": QColor(34, 197, 94),
            "failed": QColor(239, 68, 68),
            "timed_out": QColor(249, 115, 22),
        }

        for i, task in enumerate(self._tasks[:10]):
            base_y = self._COLLAPSED_H + self._ROW_H * i

            # Line 1: project + status + age
            line1_y = base_y + 12
            p.setPen(QColor(180, 180, 200))
            p.drawText(8, line1_y, task.project)

            color = STATUS_COLORS.get(task.status, QColor(100, 100, 120))
            p.setPen(color)
            p.drawText(120, line1_y, task.status)

            age = task.age_str()
            if age:
                p.setPen(QColor(100, 100, 120))
                p.drawText(w - fm.horizontalAdvance(age) - 8, line1_y, age)

            # Line 2: truncated prompt
            line2_y = base_y + 24
            small_font = QFont("sans-serif", 7)
            p.setFont(small_font)
            sfm = p.fontMetrics()
            prompt_text = self._truncate_prompt(task.prompt, sfm, w - 16)
            p.setPen(QColor(130, 130, 150))
            p.drawText(8, line2_y, prompt_text)

            # Restore font for next row
            p.setFont(font)

        p.end()


class ComfyUIRow(QWidget):
    """Shows ComfyUI status and queue info."""

    _COLLAPSED_H = 22
    _EXPANDED_H = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = False
        self._status: ComfyUIStatus | None = None
        self.setFixedHeight(self._COLLAPSED_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_data(self, status: ComfyUIStatus):
        self._status = status
        self.update()

    def mousePressEvent(self, event):
        self._expanded = not self._expanded
        self.setFixedHeight(self._EXPANDED_H if self._expanded else self._COLLAPSED_H)
        parent = self.parent()
        while parent:
            if hasattr(parent, "adjustSize"):
                parent.adjustSize()
                break
            parent = parent.parent() if hasattr(parent, "parent") else None
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        font = QFont("sans-serif", 8)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        fm = p.fontMetrics()

        y = 14
        arrow = "\u25be" if self._expanded else "\u25b8"
        p.setPen(QColor(100, 100, 120))
        p.drawText(4, y, f"COMFYUI {arrow}")

        s = self._status
        if s is None or not s.running:
            # Status dot + "Stopped"
            p.setBrush(QColor(239, 68, 68))
            p.setPen(Qt.PenStyle.NoPen)
            dot_x = fm.horizontalAdvance(f"COMFYUI {arrow}  ") + 4
            p.drawEllipse(int(dot_x), 7, 8, 8)
            p.setPen(QColor(239, 68, 68))
            p.drawText(int(dot_x) + 12, y, "Stopped")
        else:
            dot_x = fm.horizontalAdvance(f"COMFYUI {arrow}  ") + 4
            p.setBrush(QColor(34, 197, 94))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(int(dot_x), 7, 8, 8)
            p.setPen(QColor(34, 197, 94))
            p.drawText(int(dot_x) + 12, y, "Running")

            # Queue info right-aligned
            if s.queue_running > 0:
                q_text = f"generating: {s.queue_running}"
                p.setPen(QColor(59, 130, 246))
            elif s.queue_pending > 0:
                q_text = f"queued: {s.queue_pending}"
                p.setPen(QColor(234, 179, 8))
            else:
                q_text = "idle"
                p.setPen(QColor(100, 100, 120))
            p.drawText(w - fm.horizontalAdvance(q_text) - 4, y, q_text)

        if self._expanded and s and s.running:
            p.setPen(QColor(160, 160, 180))
            detail = f"Queue: {s.queue_pending} pending, {s.queue_running} running"
            p.drawText(8, y + 22, detail)

        p.end()


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class OllamaWidget(QWidget):
    """Translucent always-on-top widget displaying Ollama status."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(340)

        self._drag_pos = QPoint()
        self._status = OllamaStatus()
        self._loaded_models: list[LoadedModel] = []
        self._all_models: list[ModelInfo] = []
        self._gpu = GpuMetrics()
        self._tasks: list[TaskInfo] = []
        self._last_fetch_at: float = 0.0

        self._status_worker: OllamaStatusWorker | None = None
        self._model_worker: ModelListWorker | None = None
        self._gpu_worker: GpuWorker | None = None
        self._task_worker: TaskWorker | None = None
        self._comfyui_worker: ComfyUIWorker | None = None

        self._build_ui()
        self._setup_tray_icon()
        self._setup_timers()

        # Initial fetches
        self._fetch_status()
        self._fetch_models()
        self._fetch_gpu()
        self._fetch_tasks()
        self._fetch_comfyui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(4)

        # Header row
        header = QHBoxLayout()
        self._title_label = QLabel("OLLAMA")
        title_font = QFont("sans-serif", 13)
        title_font.setWeight(QFont.Weight.Bold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        self._title_label.setFont(title_font)
        self._title_label.setStyleSheet("color: #d4a574;")
        header.addWidget(self._title_label)
        header.addStretch()

        self._minimize_btn = QLabel("\u2013")
        self._minimize_btn.setStyleSheet(
            "color: #666680; font-size: 14px; padding: 2px 6px;"
        )
        self._minimize_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._minimize_btn.mousePressEvent = lambda _: self.hide_to_tray()
        header.addWidget(self._minimize_btn)

        close_btn = QLabel("\u2715")
        close_btn.setStyleSheet(
            "color: #666680; font-size: 14px; padding: 2px 6px;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.mousePressEvent = lambda _: self.close()
        header.addWidget(close_btn)
        layout.addLayout(header)

        _add_separator(layout)
        layout.addSpacing(4)

        # Status row
        self._status_row = StatusRow()
        layout.addWidget(self._status_row)

        _add_separator(layout)
        layout.addSpacing(2)

        # Loaded models section
        self._models_label = QLabel("LOADED MODELS")
        self._models_label.setStyleSheet(
            "color: #646478; font-size: 9px; letter-spacing: 1px;"
        )
        layout.addWidget(self._models_label)

        self._model_cards_container = QVBoxLayout()
        self._model_cards_container.setSpacing(2)
        layout.addLayout(self._model_cards_container)

        self._no_models_label = QLabel("No models loaded")
        self._no_models_label.setStyleSheet("color: #646478; font-size: 9px; padding-left: 4px;")
        layout.addWidget(self._no_models_label)

        # Available models count
        self._avail_label = QLabel("")
        self._avail_label.setStyleSheet("color: #646478; font-size: 9px; padding-left: 4px;")
        layout.addWidget(self._avail_label)

        _add_separator(layout)
        layout.addSpacing(2)

        # GPU row
        self._gpu_label = QLabel("GPU")
        self._gpu_label.setStyleSheet(
            "color: #646478; font-size: 9px; letter-spacing: 1px;"
        )
        layout.addWidget(self._gpu_label)
        self._gpu_row = GpuRow()
        layout.addWidget(self._gpu_row)

        _add_separator(layout)
        layout.addSpacing(2)

        # ComfyUI row
        self._comfyui_row = ComfyUIRow()
        layout.addWidget(self._comfyui_row)

        _add_separator(layout)
        layout.addSpacing(2)

        # Tasks section (collapsible)
        self._tasks_section = TasksSection()
        layout.addWidget(self._tasks_section)

        layout.addSpacing(2)
        _add_separator(layout)

        # Status footer
        status_layout = QHBoxLayout()
        self._footer_label = QLabel("Fetching...")
        self._footer_label.setStyleSheet("color: #666680; font-size: 10px;")
        status_layout.addWidget(self._footer_label)
        status_layout.addStretch()

        refresh_btn = QLabel("\u27f3")
        refresh_btn.setStyleSheet(
            "color: #666680; font-size: 16px; padding: 0 4px;"
        )
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.mousePressEvent = lambda _: self._refresh_all()
        status_layout.addWidget(refresh_btn)

        layout.addLayout(status_layout)

    def _setup_timers(self):
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._fetch_status)
        self._status_timer.start(STATUS_INTERVAL_MS)

        self._model_timer = QTimer(self)
        self._model_timer.timeout.connect(self._fetch_models)
        self._model_timer.start(MODEL_LIST_INTERVAL_MS)

        self._gpu_timer = QTimer(self)
        self._gpu_timer.timeout.connect(self._fetch_gpu)
        self._gpu_timer.start(GPU_INTERVAL_MS)

        self._task_timer = QTimer(self)
        self._task_timer.timeout.connect(self._fetch_tasks)
        self._task_timer.start(TASK_INTERVAL_MS)

        self._comfyui_timer = QTimer(self)
        self._comfyui_timer.timeout.connect(self._fetch_comfyui)
        self._comfyui_timer.start(COMFYUI_INTERVAL_MS)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._countdown_timer.start(COUNTDOWN_INTERVAL_MS)

    # -- Fetch methods --

    def _fetch_status(self):
        if self._status_worker and self._status_worker.isRunning():
            return
        self._status_worker = OllamaStatusWorker()
        self._status_worker.finished.connect(self._on_status_fetched)
        self._status_worker.start()

    def _on_status_fetched(self, status: OllamaStatus, loaded: list):
        self._status = status
        self._loaded_models = loaded
        self._last_fetch_at = time.time()
        self._status_row.set_data(status.running, status.version)
        self._update_model_cards()
        self._update_footer()

    def _fetch_models(self):
        if self._model_worker and self._model_worker.isRunning():
            return
        self._model_worker = ModelListWorker()
        self._model_worker.finished.connect(self._on_models_fetched)
        self._model_worker.start()

    def _on_models_fetched(self, models: list):
        self._all_models = models
        loaded_names = {m.name for m in self._loaded_models}
        idle_count = sum(1 for m in models if m.name not in loaded_names)
        if models:
            self._avail_label.setText(
                f"{len(models)} available ({idle_count} idle)"
            )
            self._avail_label.show()
        else:
            self._avail_label.hide()

    def _fetch_gpu(self):
        if self._gpu_worker and self._gpu_worker.isRunning():
            return
        self._gpu_worker = GpuWorker()
        self._gpu_worker.finished.connect(self._on_gpu_fetched)
        self._gpu_worker.start()

    def _on_gpu_fetched(self, metrics: GpuMetrics):
        self._gpu = metrics
        self._gpu_row.set_data(metrics)
        # Update model cards with actual GPU total
        self._update_model_cards()

    def _fetch_tasks(self):
        if self._task_worker and self._task_worker.isRunning():
            return
        self._task_worker = TaskWorker()
        self._task_worker.finished.connect(self._on_tasks_fetched)
        self._task_worker.start()

    def _on_tasks_fetched(self, tasks: list):
        self._tasks = tasks
        self._tasks_section.set_data(tasks)
        self.adjustSize()

    def _fetch_comfyui(self):
        if self._comfyui_worker and self._comfyui_worker.isRunning():
            return
        self._comfyui_worker = ComfyUIWorker()
        self._comfyui_worker.finished.connect(self._on_comfyui_fetched)
        self._comfyui_worker.start()

    def _on_comfyui_fetched(self, status: ComfyUIStatus):
        self._comfyui_row.set_data(status)
        self.adjustSize()

    def _refresh_all(self):
        self._fetch_status()
        self._fetch_models()
        self._fetch_gpu()
        self._fetch_tasks()
        self._fetch_comfyui()

    def _update_model_cards(self):
        # Clear existing cards
        while self._model_cards_container.count():
            item = self._model_cards_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        gpu_total = self._gpu.mem_total_gb if self._gpu.available else 24.0

        if self._loaded_models:
            self._no_models_label.hide()
            for model in self._loaded_models:
                card = ModelCard()
                card.set_data(model, gpu_total)
                self._model_cards_container.addWidget(card)
        else:
            if self._status.running:
                self._no_models_label.setText("No models loaded")
            else:
                self._no_models_label.setText("Service unavailable")
            self._no_models_label.show()

        self.adjustSize()

    def _update_countdown(self):
        self._update_footer()

    def _update_footer(self):
        if self._last_fetch_at <= 0:
            return
        elapsed = int(time.time() - self._last_fetch_at)
        self._footer_label.setText(f"Updated {elapsed}s ago")
        self._footer_label.setStyleSheet("color: #666680; font-size: 10px;")

    # -- Window chrome --

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        p.fillPath(path, QColor(20, 20, 30, 200))
        p.setPen(QPen(QColor(80, 80, 100, 60), 1))
        p.drawPath(path)
        p.end()

    def _setup_tray_icon(self):
        px = QPixmap(64, 64)
        px.fill(QColor(0, 0, 0, 0))
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(34, 197, 94))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(4, 4, 56, 56)
        p.setPen(QPen(QColor(255, 255, 255), 3))
        p.setFont(QFont("sans-serif", 28, QFont.Weight.Bold))
        p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "O")
        p.end()

        icon = QIcon(px)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Ollama Indicator")
        self._tray.activated.connect(self._on_tray_activated)

        menu = QMenu()
        self._show_hide_action = QAction("Show/Hide", self)
        self._show_hide_action.triggered.connect(self._toggle_from_tray)
        menu.addAction(self._show_hide_action)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self._refresh_all)
        menu.addAction(refresh_action)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_from_tray()

    def _show_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        self.hide()

    def _toggle_from_tray(self):
        if self.isVisible():
            self.hide_to_tray()
        else:
            self._show_from_tray()

    def closeEvent(self, event):
        event.ignore()
        self.hide_to_tray()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Ollama Indicator")
    app.setQuitOnLastWindowClosed(False)

    widget = OllamaWidget()
    widget.show()

    # Position at bottom-right of screen with padding
    screen = app.primaryScreen().geometry()
    widget.move(
        screen.width() - widget.width() - 20,
        screen.height() - widget.height() - 60,
    )

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
