# citas-monitor

Vigila la agenda de médicos de Vithas, Quirónsalud y Viamed (con un hospital y un seguro concretos) y avisa por Telegram cuando salen huecos para reservar.

- `monitor.py`: consulta las APIs de las 3 webs de citas, avisa solo de huecos nuevos (con `max_days` o un rango de fechas `date_from`/`date_to`, opcionales) y de fallos repetidos, y mantiene un mensaje silencioso de "sigo buscando".
- `bot.py` + `catalog.py`: bot privado para la familia (tú das acceso a cada persona). Con `/nueva` cada uno busca a su médico por el apellido, elige seguro, tipo de cita y plazo (N días o fechas concretas); `/mis` las lista y borra. Revisa todo cada 10 minutos y guarda los datos en `data/` (SQLite).
- `run.sh` + `.tg-service`: arrancan el bot sin Docker dentro del contenedor del agente (el bot de Telegram lo relanza al arrancar y cada 5 min; log en `data/bot.log`).
- `Dockerfile`, `docker-compose.yml`, `citas-monitor.service`, `instalar.sh`: el modo anterior en Docker + systemd (se apaga al pasar a `run.sh`).
- `.github/workflows/monitor.yml` + `monitor_config.json`: el modo anterior (GitHub Actions cada 10 min con el secreto `MONITOR_CONFIG`). El bot no lo usa; hay que apagar el workflow.
