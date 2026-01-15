#!/usr/bin/env bash

cd "$(dirname "$0")"

LOGFILE=../../../../../shared/logs/led_strip_daemon.log

# Make sure log will be readable by non-root users
touch $LOGFILE
chmod 664 $LOGFILE

. .venv/bin/activate
python3 led_strip_daemon.py $@ > $LOGFILE 2>&1
