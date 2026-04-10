# Ollama Indicator — Design Spec

## Context

Sam runs a local Ollama instance (RTX 4090) for LLM inference, used by clawd-bot's autonomous runner and ad-hoc local tasks. There's no at-a-glance visibility into whether Ollama is running, which models are loaded, GPU utilization, or clawd-bot task activity. The existing claude-indicator widget proves this overlay pattern works well on his GNOME desktop.

## Overview

A PySide6 desktop overlay widget that monitors the local Ollama service and displays clawd-bot Ollama task activity. Matches the claude-indicator visual style exactly — same dark translucent overlay, same color palette, same 340px width. Positioned bottom-right.

Published as a public GitHub repo.

## Architecture

**Single-file application**: `ollama_widget.py` — follows the claude-indicator pattern of one monolithic widget file.

**Dependencies**:
- `PySide6 >= 6.6.0` — Qt6 widget framework
- `requests >= 2.31.0` — HTTP client for Ollama API
- `boto3 >= 1.34.0` — AWS SDK for DynamoDB queries

**No authentication required** — Ollama API is unauthenticated on localhost. AWS credentials come from the default credential chain (~/.aws/credentials).

## Data Sources

| Data | Source | Endpoint/Method | Refresh Interval |
|------|--------|-----------------|------------------|
| Ollama status | `GET http://localhost:11434/api/version` | HTTP | 10s |
| Loaded models | `GET http://localhost:11434/api/ps` | HTTP | 10s |
| Available models | `GET http://localhost:11434/api/tags` | HTTP | 60s |
| GPU metrics | `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits` | Subprocess | 3s |
| clawd-bot tasks | DynamoDB `clawd-bot-tasks` table, query `provider-submitted-index` where `provider=ollama`, limit 10, descending | boto3 | 60s |

All data fetching runs in background QThreads to avoid blocking the UI. Each data source has its own worker thread and QTimer.

## Widget Layout (top to bottom)

### 1. Header
- "OLLAMA" centered in gold (#d4a574), 13px bold, letter-spacing 2px
- Close button (×) top-right, dim gray, lights up on hover

### 2. Separator
- 1px line, `QColor(80, 80, 100, 60)`

### 3. Status Row
- Left: status dot (green=running, red=stopped) + "Running" / "Stopped" text
- Right: version string (e.g., "v0.20.3")
- When Ollama is unreachable: red status dot + "Stopped", models section shows "Service unavailable". GPU section continues updating independently (GPU metrics are not Ollama-dependent).

### 4. Separator

### 5. Loaded Models Section
- Label: "LOADED MODELS" in dim uppercase (9px, #646478)
- For each loaded model (from `/api/ps`):
  - Model name in purple (#8b5cf6)
  - Size in dim text (right-aligned)
  - VRAM usage bar (color-coded: green <50%, yellow 50-74%, orange 75-89%, red 90%+)
  - Expiry countdown: "expires in Xm" based on `expires_at` from `/api/ps`
- When no models loaded: "No models loaded" in dim text

### 6. Separator

### 7. GPU Row
- Label: "GPU" in dim uppercase
- VRAM bar: used/total (e.g., "18.2 / 24.0 GB")
  - Color-coded progress bar matching claude-indicator palette
- Utilization percentage
- Temperature with color coding: green <70°C, orange 70-79°C, red ≥80°C
- When nvidia-smi unavailable: "No NVIDIA GPU detected" in dim text

### 8. Separator

### 9. Tasks Section (collapsible, default collapsed)
- Header: "▶ CLAWD-BOT TASKS (N recent)" — click to expand/collapse
- Expanded shows last 10 Ollama tasks from DynamoDB:
  - Project name
  - Status badge: pending (gray), running (blue), completed (green), failed (red), timed_out (orange)
  - Relative timestamp ("2m ago", "1h ago")
- When no AWS credentials or DynamoDB unreachable: "AWS unavailable" in dim text (non-fatal)

### 10. Separator

### 11. Status Footer
- Left: "Updated Xs ago" with countdown
- Right: Manual refresh button (↻)

## Window Properties

- **Width**: 340px fixed
- **Height**: Dynamic (adjustSize)
- **Position**: Bottom-right, 20px padding from screen edges
- **Flags**: `FramelessWindowHint | WindowStaysOnTopHint | Tool`
- **Attribute**: `WA_TranslucentBackground`
- **Background**: `QColor(20, 20, 30, 200)` with 16px corner radius
- **Border**: `QColor(80, 80, 100, 60)` at 1px
- **Draggable**: Mouse press/move handlers for repositioning

## System Tray

- Tray icon with Ollama-themed icon (simple circle with status color)
- Right-click menu: Show/Hide, Refresh, Quit
- Double-click: Toggle visibility
- `setQuitOnLastWindowClosed(False)` — closing hides to tray

## Autostart

- Desktop entry at `~/.config/autostart/ollama-widget.desktop`
- Uses full Python path: `/home/sam/miniconda3/bin/python3`
- Sets `LD_LIBRARY_PATH=/home/sam/miniconda3/lib` for xcb-cursor

## Color Palette (matching claude-indicator)

| Element | Color |
|---------|-------|
| Background | `QColor(20, 20, 30, 200)` |
| Border | `QColor(80, 80, 100, 60)` |
| Title | `#d4a574` (warm gold) |
| Model accent | `#8b5cf6` (purple) |
| Primary text | `QColor(180, 180, 200)` |
| Secondary text | `QColor(100, 100, 120)` |
| Dim labels | `QColor(160, 160, 180)` |
| Bar green | `QColor(34, 197, 94)` — 0-49% |
| Bar yellow | `QColor(234, 179, 8)` — 50-74% |
| Bar orange | `QColor(249, 115, 22)` — 75-89% |
| Bar red | `QColor(239, 68, 68)` — 90-100% |
| Status running | `QColor(34, 197, 94)` (green) |
| Status stopped | `QColor(239, 68, 68)` (red) |
| Task running | `QColor(59, 130, 246)` (blue) |

## Threading Model

```
Main Thread (UI)
├── OllamaStatusWorker (QThread) — 10s timer, fetches /api/version + /api/ps
├── ModelListWorker (QThread) — 60s timer, fetches /api/tags
├── GpuWorker (QThread) — 3s timer, calls nvidia-smi
├── TaskWorker (QThread) — 60s timer, queries DynamoDB
└── CountdownTimer — 1s timer, updates "Updated Xs ago" without I/O
```

Each worker emits a signal on completion. Main thread updates UI in signal handler.

## Error Handling

- **Ollama unreachable**: Show red "Stopped" status, models section shows "Service unavailable". GPU section continues independently. Keep polling — auto-recover when Ollama starts.
- **nvidia-smi missing/fails**: Show "No NVIDIA GPU" message. Don't retry (check once on startup, then on each timer tick).
- **DynamoDB unreachable / no credentials**: Show "AWS unavailable" in tasks section. Non-fatal — rest of widget works fine.
- **All errors are silent** — no popups, no crashes. Status shown inline.

## File Structure

```
ollama-indicator/
├── ollama_widget.py          # Main application (single file)
├── requirements.txt          # PySide6, requests, boto3
├── README.md                 # Installation & usage
├── CLAUDE.md                 # Project instructions
├── TODO.md                   # Task tracking
├── .gitignore                # .superpowers/, __pycache__, etc.
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-04-09-ollama-indicator-design.md  # This file
```

## Public GitHub Repo

- Repository: `samuelfrench/ollama-indicator`
- License: MIT
- No sensitive data (no AWS account IDs, no credentials, no PII)
- README with installation instructions, screenshot, and feature list

## Verification

1. Run `python3 ollama_widget.py` — widget appears bottom-right
2. Verify Ollama status shows green "Running" with correct version
3. Load a model (`ollama run qwen3.5:35b-a3b`) — verify it appears in "Loaded Models"
4. Check GPU metrics update every 3s (VRAM, utilization, temp)
5. Expand tasks section — verify DynamoDB tasks appear with correct status badges
6. Stop Ollama (`systemctl stop ollama`) — verify widget shows red "Stopped"
7. Restart Ollama — verify auto-recovery to green "Running"
8. Drag widget — verify it moves
9. Close widget — verify it minimizes to tray
10. System tray right-click — verify Show/Hide/Refresh/Quit work
