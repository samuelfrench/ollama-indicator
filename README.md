# Ollama Indicator

Translucent desktop overlay widget for monitoring your local Ollama instance, GPU metrics, ComfyUI status, and clawd-bot task activity.

Built with PySide6 (Qt6). Designed for GNOME/Linux desktops.

## Features

- **Ollama Service Status** — Running/stopped detection with version display
- **Loaded Models** — Real-time view of models in memory with VRAM usage bars and expiry countdowns
- **GPU Metrics** — VRAM usage, utilization %, temperature (nvidia-smi)
- **ComfyUI Status** — Running/stopped with generation queue info
- **clawd-bot Tasks** — Recent Ollama task activity from DynamoDB (collapsible)
- **Always-on-top** — Frameless, translucent, draggable overlay
- **System tray** — Minimize to tray, right-click menu
- **Autostart** — Desktop entry for GNOME session startup

## Installation

```bash
git clone https://github.com/samuelfrench/ollama-indicator.git
cd ollama-indicator
pip install -r requirements.txt
python3 ollama_widget.py
```

## Requirements

- Python 3.10+
- PySide6
- Ollama running on `localhost:11434`
- NVIDIA GPU with `nvidia-smi` (optional — gracefully degrades)
- AWS credentials for DynamoDB task tracking (optional)
- ComfyUI on `localhost:8188` (optional)

## Configuration

Edit constants at the top of `ollama_widget.py`:

- `OLLAMA_URL` — Ollama API endpoint (default: `http://localhost:11434`)
- `COMFYUI_URL` — ComfyUI endpoint (default: `http://127.0.0.1:8188`)
- `DYNAMO_TABLE` — DynamoDB table name for task tracking
- Refresh intervals for each data source

## Autostart

Copy the desktop entry to autostart:

```bash
cp ~/.config/autostart/ollama-widget.desktop ~/.config/autostart/
```

Or create one with your Python path:

```ini
[Desktop Entry]
Type=Application
Name=Ollama Indicator
Exec=python3 /path/to/ollama_widget.py
Terminal=false
X-GNOME-Autostart-enabled=true
```

## License

MIT
