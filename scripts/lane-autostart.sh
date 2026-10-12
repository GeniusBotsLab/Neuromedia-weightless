#!/bin/bash
# @reboot hook: docker restart=unless-stopped reuses the container env baked at launch.
# RoCE GID indices move at each boot, so after a reboot the lane must be relaunched
# through its start script (which re-resolves GIDs), not merely restarted.
# Keep in sync with scripts/lane_watchdog.py (the cron watchdog uses the same map).
LANE=$(cat $HOME/current-lane 2>/dev/null || echo dsv4vl)
case "$LANE" in
  dsv4vl) DIR=$HOME/dspark-dsv4vl; SCRIPT=start-dsv4-vl-dspark.sh ;;
  dsv4) DIR=$HOME/dspark-miaai; SCRIPT=start-deepseek-v4-flash-dspark.sh ;;
  glm53tp4) DIR=$HOME; SCRIPT=$HOME/tp4-bringup/launch-tp4.sh ;;
  *) echo "unknown lane $LANE" >> $HOME/lane-autostart.log; exit 0 ;;
esac
for i in $(seq 1 30); do
  C=$(cat /sys/class/net/enp1s0f1np1/carrier 2>/dev/null)
  docker info >/dev/null 2>&1 && [ "$C" = "1" ] && break
  sleep 10
done
sleep 20
echo "$(date -Is) relaunching lane $LANE" >> $HOME/lane-autostart.log
cd "$DIR" && bash "$SCRIPT" >> $HOME/lane-autostart.log 2>&1
