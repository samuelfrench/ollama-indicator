import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtWidgets import QApplication

import ollama_widget


@pytest.fixture(scope="session")
def app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def widget(monkeypatch, app):
    monkeypatch.setattr(ollama_widget.OllamaWidget, "_setup_timers", lambda self: None)
    monkeypatch.setattr(ollama_widget.OllamaWidget, "_fetch_status", lambda self: None)
    monkeypatch.setattr(ollama_widget.OllamaWidget, "_fetch_models", lambda self: None)
    monkeypatch.setattr(ollama_widget.OllamaWidget, "_fetch_gpu", lambda self: None)
    monkeypatch.setattr(ollama_widget.OllamaWidget, "_fetch_tasks", lambda self: None)
    monkeypatch.setattr(ollama_widget.OllamaWidget, "_fetch_comfyui", lambda self: None)
    w = ollama_widget.OllamaWidget()
    yield w
    w.close()
    w.deleteLater()


def test_header_minimize_button_hides_to_tray(widget, monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "hide_to_tray", lambda: calls.append("hide"))

    widget._minimize_btn.mousePressEvent(None)

    assert calls == ["hide"]


def test_close_event_uses_hide_to_tray(widget, monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "hide_to_tray", lambda: calls.append("hide"))

    class Event:
        ignored = False

        def ignore(self):
            self.ignored = True

    event = Event()
    widget.closeEvent(event)

    assert event.ignored is True
    assert calls == ["hide"]


def test_tray_show_hide_action_toggles_visibility(widget):
    action = widget._show_hide_action
    assert action.text() == "Show/Hide"

    widget.show()
    action.trigger()
    assert not widget.isVisible()

    action.trigger()
    assert widget.isVisible()
