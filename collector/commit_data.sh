#!/usr/bin/env bash
# Коммитит data/ после сбора и отправляет в main.
#
# Источник правды — ответы API в data/<клуб>/raw/: у каждого замера свой файл, они не конфликтуют.
# occupancy.csv и SUMMARY.md — производные, collector/build.py пересобирает их из всех raw.
# Если за время сбора main ушёл вперёд (другой запуск успел отправить свои замеры) и rebase
# упирается в конфликт в производных файлах, берём свежий main, возвращаем в него свои новые
# файлы и пересобираем производные файлы клубов. Так не теряются ни свои, ни чужие замеры.
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898099+github-actions[bot]@users.noreply.github.com"

git add -A data
if git diff --cached --quiet; then
  echo "Нет новых данных"
  exit 0
fi
msg="data: $(date -u +%FT%TZ)"
git commit -q -m "$msg"
ours=$(git rev-parse HEAD)
base=$(git rev-parse HEAD~1)

# Свой коммит заново поверх $1: новые файлы — свои (если в main такого файла ещё нет),
# остальное — как в main, производные файлы — пересборкой из raw.
rebuild_on() {
  local f
  local -a clubs
  git reset -q --hard "$1"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    [ -e "$f" ] && continue   # уже есть в main (например, тарифы на завтра) — оставляем версию main
    git checkout -q "$ours" -- "$f"
  done < <(git diff --name-only --diff-filter=A "$base" "$ours" -- data)
  mapfile -t clubs < <(git diff --name-only --diff-filter=A "$base" "$ours" -- 'data/*/raw/*' | cut -d/ -f2 | sort -u)
  for c in "${clubs[@]}"; do
    echo "пересобираю $c"
    python3 collector/build.py "$c"
  done
  git add -A data
  if git diff --cached --quiet; then
    echo "После пересборки поверх main нового нет"
    return 0
  fi
  git commit -q -m "$msg (пересобрано поверх main)"
}

for i in 1 2 3 4 5; do
  git fetch -q origin main
  if ! git rebase -q origin/main; then
    echo "Конфликт при rebase на origin/main — пересобираю производные файлы из raw"
    git rebase --abort
    rebuild_on origin/main
  fi
  if git push -q origin HEAD:main; then
    echo "Отправлено: $(git rev-parse --short HEAD)"
    exit 0
  fi
  echo "push отклонён, попытка $i"
  sleep $((i * 5))
done
echo "Не удалось отправить данные после 5 попыток" >&2
exit 1
