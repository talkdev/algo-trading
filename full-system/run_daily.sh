#!/bin/bash
# run_daily.sh — Run at 15:35 IST every trading day
# Add to crontab: 35 15 * * 1-5 /path/to/run_daily.sh

cd C:\Users\Administrator\Desktop\algo-trading\full-system

# Generate today's journal and LLM prompt
python decision_journal.py --date $(date +%Y-%m-%d) --output-dir journals

# Weekly cumulative analysis (runs every Friday)
if [ $(date +%u) -eq 5 ]; then
    python decision_journal.py --cumulative --lookback 30 --output-dir journals
    echo "Weekly cumulative analysis generated"
fi

echo "Daily journal complete. Check journals/ directory."
echo "Paste llm_prompt_$(date +%Y-%m-%d).txt into your LLM."