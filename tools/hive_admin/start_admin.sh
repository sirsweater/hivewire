#!/bin/bash
# (Re)start the hive admin page in the background. Meant for an @reboot
# crontab entry, so it needs no root:
#
#   @reboot sleep 20 && $HOME/hivewire-tools/hive_admin/start_admin.sh
#
# HIVE_GW must name the gateway by its stable by-id path, never /dev/ttyACMn --
# that numbering changes when USB devices re-enumerate.
HERE="$(cd "$(dirname "$0")" && pwd)"
GW="${HIVE_GW:?set HIVE_GW to /dev/serial/by-id/usb-..._<gateway MAC>-if00}"
DATA="${HIVE_DATA:-$HOME/hive_data}"
# Tools that use the gateway port directly; the admin stays off it while one runs.
YIELD='[h]ivewire_push.py|[t]est_(rollback|family).py|[b]urn_push.py|[g]w_cmd.py|[p]ush_std.py'

pkill -f 'python3 [^ ]*hive_admin\.py' 2>/dev/null
sleep 1
mkdir -p "$DATA"
setsid nohup python3 "$HERE/hive_admin.py" --port "$GW" --data "$DATA" \
  --push-script "$HERE/../hivewire_push.py" --yield-to "$YIELD" \
  >> "$DATA/admin.log" 2>&1 < /dev/null &
echo "hive admin starting: http://$(hostname -I | awk '{print $1}'):8080/"
