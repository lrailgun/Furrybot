#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID != 0 ]]; then echo 'Запуск: sudo bash install.sh' >&2; exit 1; fi
for cmd in git python3 systemctl useradd getent; do command -v "$cmd" >/dev/null || { echo "Отсутствует $cmd" >&2; exit 1; }; done
if [[ ! -d /run/systemd/system ]]; then echo 'Требуется Linux с systemd.' >&2; exit 1; fi
APP=/opt/furrybot
if [[ -e "$APP" ]]; then echo "$APP уже существует; используй furrybot --update или проверь незавершённую установку." >&2; exit 1; fi
getent group furrybot >/dev/null || groupadd --system furrybot
id furrybot >/dev/null 2>&1 || useradd --system --gid furrybot --home-dir /var/lib/furrybot --shell /usr/sbin/nologin furrybot
install -d -m 0750 -o root -g furrybot /etc/furrybot
install -d -m 0700 -o furrybot -g furrybot /var/lib/furrybot
git clone --branch main --single-branch https://github.com/lrailgun/Furrybot.git "$APP"
cd "$APP"
REV=$(git rev-parse HEAD)
python3 -m venv ".venvs/$REV"
".venvs/$REV/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
".venvs/$REV/bin/python" -m unittest discover -s tests
ln -s "$REV" .venvs/current
install -m 0644 furrybot.service /etc/systemd/system/furrybot.service
ln -sfn "$APP/cli.py" /usr/local/bin/furrybot
systemctl daemon-reload
systemctl enable furrybot.service
echo 'Установка завершена. sudo furrybot — задай секреты и запусти сервис.'
