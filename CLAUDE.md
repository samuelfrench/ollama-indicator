# Ollama Indicator

PySide6 desktop overlay widget monitoring local Ollama service + clawd-bot task activity.

## Architecture
- Single-file application: `ollama_widget.py`
- Matches claude-indicator visual style (dark translucent overlay, 340px width)
- Positioned bottom-right of screen

## Data Sources
- Ollama REST API at `http://localhost:11434` (status, loaded models, available models)
- `nvidia-smi` subprocess for GPU metrics
- AWS DynamoDB `clawd-bot-tasks` table for task activity (provider=ollama)

## Key Decisions
- Single-file pattern from claude-indicator — no module splitting
- All I/O in background QThreads, signals to main thread
- DynamoDB failures are non-fatal — widget works without AWS credentials

## Running
```bash
python3 ollama_widget.py
```

## TODO
Read `TODO.md` at start of each session.
