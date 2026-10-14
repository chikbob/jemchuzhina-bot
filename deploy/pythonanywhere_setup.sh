#!/usr/bin/env bash
set -eu

task_project="$HOME/jemchuzhena_bot"
cd "$task_project"
chmod 600 .env
python3.12 -m venv .venv
.venv/bin/pip install --disable-pip-version-check -r requirements-web.txt
.venv/bin/python -m unittest discover -s tests -q
printf 'Project ready. Configure the Web tab using deploy/pythonanywhere_wsgi.py.\n'
