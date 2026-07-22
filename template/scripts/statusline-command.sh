#!/bin/bash
# Claude Code status line
# Format: <Model> | Ctx <used>% | 5h <used>% | 7d <used>%

input=$(cat)

model=$(echo "$input" | jq -r '.model.display_name')

ctx=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
five=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
week=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')

out="$model"

if [ -n "$ctx" ]; then
  out="$out | Ctx $(printf '%.0f' "$ctx")%"
fi

if [ -n "$five" ]; then
  out="$out | 5h $(printf '%.0f' "$five")%"
fi

if [ -n "$week" ]; then
  out="$out | 7d $(printf '%.0f' "$week")%"
fi

printf '%s' "$out"
