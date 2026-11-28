#!/bin/sh
# Installs the two cron lines for THIS clone (pipeline at minute 7, monitor at minute 20, every hour).
# Safe to run twice: it replaces its own lines and keeps everything else in your crontab.
# Run it from the Python environment the project uses (it records that python's full path).
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-$(command -v python3)}
mkdir -p "$REPO/logs"
cat > "$REPO/ops/crontab.txt" <<LINES
# m h dom mon dow command   (cron uses the machine's local time; the pipeline's own clock and its log are UTC)
7 * * * * cd $REPO && $PY src/run_pipeline.py >> logs/pipeline.log 2>&1
20 * * * * cd $REPO && $PY src/monitor.py >> logs/monitor.log 2>&1
LINES
{ crontab -l 2>/dev/null | grep -v "src/run_pipeline.py" | grep -v "src/monitor.py" || true; grep -v '^#' "$REPO/ops/crontab.txt"; } | crontab -
echo "installed. crontab -l shows:"
crontab -l
