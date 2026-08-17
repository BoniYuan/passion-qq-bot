@echo off
title Check Passion Bot Server Status
echo Enter the server password when prompted.
echo.
ssh root@43.134.235.139 "echo SYSTEM; uptime; echo; echo DOCKER_SERVICE; systemctl is-enabled docker; systemctl is-active docker; echo; echo CONTAINERS; cd /opt/passion-bot && docker compose ps; echo; echo RESTART_COUNTS; docker inspect -f '{{.Name}} status={{.State.Status}} restarts={{.RestartCount}} started={{.State.StartedAt}}' sub2-astrbot sub2-napcat; echo; echo ASTRBOT_LOGS; docker logs --tail 80 sub2-astrbot; echo; echo NAPCAT_LOGS; docker logs --tail 80 sub2-napcat; echo; echo DISK; df -h /"
echo.
echo Status check finished. Take screenshots of this window.
pause
