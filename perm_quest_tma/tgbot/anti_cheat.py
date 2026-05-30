import datetime
import math
import logging
from typing import Tuple, Optional

# Настройка логгера для модуля античета
logger = logging.getLogger(__name__)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Вычисляет ортодромическое расстояние (по дуге большого круга) между двумя 
    точками на поверхности Земли в метрах с использованием формулы гаверсинусов.
    """
    # Средний радиус Земли в метрах
    R = 6371000.0

    # Перевод координат из градусов в радианы
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    # Формула гаверсинусов
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return R * c


def verify_gps_and_speed(
    current_lat: float,
    current_lon: float,
    target_lat: float,
    target_lon: float,
    prev_lat: Optional[float],
    prev_lon: Optional[float],
    prev_time: Optional[datetime.datetime],
    max_distance_error: float = 30.0,  # Допустимый радиус погрешности в метрах
    max_speed_mps: float = 15.0       # Лимит скорости движения по умолчанию (в м/с)
) -> Tuple[bool, str]:
    """
    Выполняет комплексный анализ геоданных игрока.
    
    Проверяет:
    1. Нахождение в радиусе целевой точки с учетом погрешности (30 метров).
    2. Скорость перемещения от предыдущей подтвержденной точки (Античит).
       Если скорость выше установленного лимита квеста, это расценивается как использование 
       симулятора Fake GPS или перемещение на высокоскоростном транспорте.

    Возвращает:
        (is_valid, message) - статус валидности и текстовое пояснение/ошибку.
    """
    # 1. Сверяем расстояние до целевой точки квеста
    distance_to_target = haversine_distance(current_lat, current_lon, target_lat, target_lon)
    
    if distance_to_target > max_distance_error:
        logger.info(
            f"Игрок слишком далеко. До цели: {distance_to_target:.1f}м. "
            f"Требуется: <= {max_distance_error}м."
        )
        return False, (
            f"📍 Вы еще не дошли до цели. Текущее расстояние до точки: {int(distance_to_target)} метров. "
            f"Подойдите ближе (требуется радиус до {int(max_distance_error)}м) и попробуйте снова!"
        )

    # 2. Проверка античета по скорости перемещения
    if prev_lat is not None and prev_lon is not None and prev_time is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Безопасное приведение naive-datetime из БД к aware UTC во избежание TypeError
        if prev_time.tzinfo is None:
            prev_time = prev_time.replace(tzinfo=datetime.timezone.utc)

        time_diff = (now - prev_time).total_seconds()

        # 🚀 УЛУЧШЕНИЕ: Игнорируем проверку скорости при дельте времени < 1.0 сек
        # Это исключает ложные срабатывания при дублировании сообщений в Telegram
        if time_diff < 1.0:
            logger.info(
                f"Античит-анализ пропущен: дельта времени ({time_diff:.2f}с) слишком мала."
            )
            return True, "Успешно! Вы прибыли на контрольную точку (проверка скорости пропущена)."

        # Вычисляем пройденное расстояние от предыдущей контрольной точки
        distance_from_prev = haversine_distance(prev_lat, prev_lon, current_lat, current_lon)
        calculated_speed = distance_from_prev / time_diff

        logger.info(
            f"Античит-анализ: расстояние от прошлой точки={distance_from_prev:.1f}м, "
            f"время={time_diff:.1f}с, скорость={calculated_speed:.2f} м/с (лимит: {max_speed_mps:.2f} м/с)."
        )

        # Если скорость превышает лимит конкретного квеста, фиксируем читерство
        if calculated_speed > max_speed_mps:
            speed_kmh = calculated_speed * 3.6
            limit_kmh = max_speed_mps * 3.6
            logger.warning(
                f"🚨 Обнаружен триггер античита! Превышена скорость движения: "
                f"{calculated_speed:.2f} м/с ({speed_kmh:.1f} км/ч) при лимите {limit_kmh:.1f} км/ч."
            )
            return False, (
                f"🚨 *Обнаружена аномалия GPS!*\n\n"
                f"Система зафиксировала нереалистичную скорость перемещения: "
                f"*{round(speed_kmh, 1)} км/ч* при лимите для этого квеста *{round(limit_kmh, 1)} км/ч*.\n"
                f"Использование Fake GPS или высокоскоростного транспорта запрещено правилами текущего квеста!"
            )

    return True, "Успешно! Вы прибыли на контрольную точку."