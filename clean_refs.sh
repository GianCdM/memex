#!/bin/bash
find /root/src/memex -type f -name "*.py" -o -name "*.md" | while read f; do
    # Replace ollama references
    sed -i 's/claude CLI \/ ollama/claude CLI, OpenRouter/g' "$f"
    sed -i 's/(claude CLI \/ ollama)/(claude CLI, OpenRouter)/g' "$f"
done
echo "done"