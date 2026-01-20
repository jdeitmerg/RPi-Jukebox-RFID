#!/usr/bin/env bash

cd "$(dirname "$0")"

INSTALL_DIR=../../../../..
LOGFILE=$INSTALL_DIR/shared/logs/led_strip_daemon.log

# Make sure log will be readable by non-root users
touch $LOGFILE
chmod 664 $LOGFILE

. $INSTALL_DIR/.venv/bin/activate
python3 led_strip_daemon.py $@ 2>&1 | tee $LOGFILE
