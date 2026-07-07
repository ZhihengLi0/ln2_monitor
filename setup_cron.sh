#!/bin/bash
# Install the LN2 ingest + monitor cron jobs (idempotent)
( crontab -l 2>/dev/null | grep -v "ln2_monitor";
  echo "* * * * * python3 /home/cdms/ln2_monitor/ingest.py  >> /home/cdms/ln2_monitor/ingest.log  2>&1";
  echo "* * * * * python3 /home/cdms/ln2_monitor/monitor.py  >> /home/cdms/ln2_monitor/monitor.log 2>&1"
) | crontab -
echo "LN2 cron jobs installed."
crontab -l | grep ln2_monitor
