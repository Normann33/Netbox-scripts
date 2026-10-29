import os
import keyring
import requests
import pynetbox
import csv
import io
import socket
from dotenv import load_dotenv

load_dotenv()

netdisco_url = os.getenv("NETDISCO_URL")
netdisco_username = os.getenv("NETDISCO_USER")
netdisco_password = keyring.get_password("Netdisco", netdisco_username)

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

# --- НАСТРОЙКИ NETBOX ---
NETBOX_URL = netbox_url
NETBOX_TOKEN = netbox_token

# --- НАСТРОЙКИ NETDISCO ---
NETDISCO_URL = netdisco_url
NETDISCO_USER = netdisco_username       
NETDISCO_PASS = netdisco_password       

# --- НАСТРОЙКИ ПАПКИ С КОНФИГАМИ ---
RAW_CONFIGS_DIR = "~/backbone/configs"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ""  # ПОПРАВКА: Конфиги хранятся БЕЗ расширения


def get_netdisco_session():
    """Авторизуется в Netdisco по логину/паролю из .env и keyring"""
    login_url = f"{NETDISCO_URL}/login"
    try:
        response = requests.post(
            login_url,
            auth=(NETDISCO_USER, NETDISCO_PASS),
            headers={'Accept': 'application/json'}
        )
        if response.status_code == 200:
            try:
                return response.json().get("api_key")
            except ValueError:
                return response.text.strip()
        else:
            print(f"🛑 Ошибка входа в Netdisco. Статус: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"🛑 Ошибка подключения к Netdisco: {e}")
        return None


def resolve_dns(device_name):
    """Резолвит имя устройства в IP для точечного запроса к Netdisco"""
    try:
        return socket.gethostbyname(device_name)
    except socket.gaierror:
        return None


def main():
    # 1. Авторизация в Netdisco
    api_token = get_netdisco_session()
    if not api_token:
        print("🛑 Синхронизация отменена из-за ошибки авторизации.")
        return

    req_headers = {"Authorization": api_token}
    print("✅ Успешная авторизация в Netdisco.")

    # 2. Инициализация NetBox
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    nb_devices = nb.dcim.devices.filter(status="active")
    
    # Заранее находим или проверяем существование роли Switch в NetBox
    # В NetBox 4.x поиск роли идет по slug
    switch_role = nb.dcim.device_roles.get(slug="switch")
    if not switch_role:
        print("⚠️  Предупреждение: Роль с кодовым именем 'switch' не найдена в NetBox.")
        print("    Убедитесь, что в NetBox UI -> Device Roles создана роль Switch со slug 'switch'.")
        return

    print("\n🔄 Начинаем комплексное обновление типов и ролей устройств...")
    
    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()

        # Проверяем наличие локального конфига БЕЗ расширения
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
        if not os.path.exists(file_path):
            print(f"⏭️  {device_name}: конфиг не найден. Пропускаем.")
            continue

        # Переменная для отслеживания изменений по устройству
        device_updated = False

        # ПРОВЕРКА И ИСПРАВЛЕНИЕ РОЛИ (Server -> Switch)
        current_role_slug = nb_dev.role.slug if nb_dev.role else ""
        if current_role_slug == "server" or current_role_slug != "switch":
            print(f"🔄 {device_name}: Изменяем роль '{current_role_slug}' -> 'switch'")
            nb_dev.role = switch_role.id
            device_updated = True

        # ПРОВЕРКА ПРОИЗВОДИТЕЛЯ И МОДЕЛИ (Generic -> Заводской)
        current_type_name = nb_dev.device_type.model if nb_dev.device_type else ""
        current_vendor = nb_dev.device_type.manufacturer.name if (nb_dev.device_type and nb_dev.device_type.manufacturer) else ""
        
        # Если тип устройства пустой, либо производитель/модель содержат заглушку "generic"
        need_type_update = (
            not current_type_name or 
            "generic" in current_type_name.lower() or 
            "generic" in current_vendor.lower()
        )

        if need_type_update:
            # Резолвим IP для точечного запроса
            management_ip = nb_dev.primary_ip4.address.split('/')[0] if nb_dev.primary_ip4 else resolve_dns(device_name)

            if not management_ip:
                print(f"❌ {device_name}: Нет IP и не резолвится в DNS. Невозможно определить точную модель.")
                # Если роль успели поменять, сохраняем её даже без обновления типа
                if device_updated:
                    nb_dev.save()
                continue

            # 3. Запрашиваем карточку устройства из Netdisco по IP
            device_url = f"{NETDISCO_URL}/api/v1/object/device/{management_ip}"
            try:
                res = requests.get(device_url, headers=req_headers)
                if res.status_code == 200:
                    csv_text = res.content.decode('utf-8')
                    reader = csv.DictReader(io.StringIO(csv_text.strip()))
                    search_data = list(reader)
                    
                    if search_data:
                        disco_device = search_data
                        disco_model_name = disco_device.get("Model", "").strip()
                        
                        if disco_model_name:
                            print(f"🔎 {device_name}: В сети обнаружена заводская модель -> '{disco_model_name}'")
                            
                            # Ищем правильный Device Type в базе NetBox
                            netbox_device_type = nb.dcim.device_types.get(model=disco_model_name)
                            
                            if netbox_device_type:
                                # Перезаписываем старый тип Generic на корректный
                                nb_dev.device_type = netbox_device_type.id
                                device_updated = True
                                print(f"   ✅ Подготовлена замена типа на '{disco_model_name}'")
                            else:
                                print(f"   ⚠️  Шаблон '{disco_model_name}' отсутствует в NetBox. Сначала запустите import_device_types.py")
                else:
                    print(f"  ⚠️  Не удалось получить данные из Netdisco для {device_name}. Статус: {res.status_code}")
            except Exception as e:
                print(f"   ❌ Ошибка запроса к Netdisco для {device_name}: {e}")

        # Сохраняем все накопленные изменения (Роль + Тип устройства) в один проход
        if device_updated:
            try:
                nb_dev.save()
                print(f"   🎉 {device_name}: Все изменения (Роль/Тип) успешно сохранены в NetBox.")
            except Exception as e:
                print(f"   ❌ Ошибка сохранения устройства {device_name} в NetBox: {e}")
        else:
            print(f"🔹 {device_name}: Роль 'Switch' и тип '{current_type_name}' уже корректны.")

if __name__ == "__main__":
    main()