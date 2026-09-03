#!/bin/bash
# Detached post-deploy restart payload for fork-sync.sh deploy.
# Verbatim from the daily upstream-sync cron job's STEP 8. It is scheduled
# (sleep 90) so the caller's report is delivered before the restart kills
# the caller's own host process. Keep in sync with the cron job text.
sleep 90
U=$(id -u)
for l in ai.hermes.gateway-desktop-local ai.hermes.gateway-marketing-operator ai.hermes.gateway-avatar-operator ai.hermes.webui; do
  launchctl kickstart -k gui/$U/$l 2>/dev/null
done
osascript -e "tell application \"Hermes\" to quit"
sleep 8
open -a Hermes
