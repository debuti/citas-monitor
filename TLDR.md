# citas-monitor

Vigila la agenda de médicos de Vithas, Quirónsalud y Viamed (hospital y seguro concretos) y avisa por Telegram cuando salen huecos para reservar.

- `monitor.py`: consulta las APIs internas de las tres webs de citas, avisa solo de huecos nuevos (con `max_days` opcional), avisa si un médico falla 3 veces seguidas y mantiene un mensaje silencioso de "sigo buscando".
- `.github/workflows/monitor.yml`: lo ejecuta cada 10 minutos en GitHub Actions.
- Configuración (médicos y chat de Telegram de cada uno): secreto `MONITOR_CONFIG`; `subir-config.sh` comprueba `monitor_config.json` y lo sube. Ejemplo en `monitor_config.example.json`.
