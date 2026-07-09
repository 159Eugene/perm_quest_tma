#!/usr/bin/env python3
"""
Маршруты для новой админ-панели на основе карты Leaflet
Интегрируется с FastAPI для отображения веб-интерфейса

Использование в main.py:
    from backend.map_admin_routes import setup_map_admin
    setup_map_admin(app)
"""

import os
from fastapi import FastAPI
from fastapi.responses import FileResponse

def setup_map_admin(app: FastAPI):
    
    @app.get("/admin-map/")
    async def get_admin_map():
        templates_dir = os.path.join(os.path.dirname(__file__), "templates")
        admin_map_path = os.path.join(templates_dir, "admin_map.html")
        return FileResponse(
            admin_map_path,
            media_type="text/html; charset=utf-8"
        )

# Примечание: Все необходимые API эндпоинты уже существуют в main.py:
#
# 1. GET /api/admin/dicts - возвращает словари для админки
#    Возвращает: { "quests": [...], "steps": [...] }
#
# 2. GET /api/admin/radar - получить позиции активных игроков
#    Возвращает: [{ "user_id": 123, "name": "...", "lat": 58.01, "lng": 56.23, ... }]
#
# 3. GET /api/admin/heatmap/{quest_id} - тепловая карта для конкретного квеста
#    Возвращает: [{ "lat": 58.01, "lng": 56.23 }, ...]
#
# Дополнительные функции для интеграции:
#
# TODO: Создать эндпоинты для создания квестов через API
# TODO: Создать эндпоинты для создания городов через API
# TODO: Создать эндпоинты для редактирования элементов через карту
# TODO: Добавить WebSocket для live-обновления позиций игроков на карте