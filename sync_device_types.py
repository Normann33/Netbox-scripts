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
        print("🛑 Синхронизация типов устройств отменена из-за ошибки авторизации.")
        return

    req_headers = {"Authorization": api_token}
    print("✅ Успешная авторизация в Netdisco.")

    # 2. Инициализация NetBox
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    nb_devices = nb.dcim.devices.filter(status="active")
    
    print("\n🔄 Начинаем сверку моделей устройств со словарем Netdisco...")
    
    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()

        # Если у устройства в NetBox УЖЕ задан тип устройства (модель), пропускаем его
        # Мы проверяем slug или имя дефолтного/пустого типа, если он "unknown" или не заполнен
        if nb_dev.device_type and nb_dev.device_type.slug != "unknown-device": 
            # Замените "unknown-device" на slug дефолтного типа, если вы их так создавали
            print(f"🔹 {device_name}: уже имеет тип '{nb_dev.device_type.model}'. Пропускаем.")
            continue

        # Определяем IP для запроса к Netdisco
        management_ip = None
        if nb_dev.primary_ip4:
            management_ip = nb_dev.primary_ip4.address.split('/')[0]
        else:
            management_ip = resolve_dns(device_name)

        if not management_ip:
            print(f"❌ {device_name}: Нет IP и не резолвится в DNS. Пропустить определение модели.")
            continue

        # 3. Запрашиваем карточку устройства из Netdisco по точному IP
        device_url = f"{NETDISCO_URL}/api/v1/object/device/{management_ip}"
        try:
            res = requests.get(device_url, headers=req_headers)
            if res.status_code != 200:
                print(f"⚠️  Не удалось получить данные из Netdisco для {device_name} ({management_ip})")
                continue

            csv_text = res.content.decode('utf-8')
            reader = csv.DictReader(io.StringIO(csv_text.strip()))
            search_data = list(reader)
            
            if not search_data:
                print(f"❓ Устройство {device_name} не найдено в базе Netdisco по IP {management_ip}.")
                continue

            disco_device = search_data[0]
            # Извлекаем точную заводскую модель из Netdisco
            disco_model_name = disco_device.get("Model", "").strip()

            if not disco_model_name:
                print(f"⚠️  В Netdisco для {device_name} поле Model оказалось пустым.")
                continue

            print(f"🔎 {device_name}: В сети обнаружена модель -> '{disco_model_name}'")

            # 4. Ищем соответствующий Device Type в NetBox 4.x
            # Поиск идет по полю model (наименование модели)
            netbox_device_type = nb.dcim.device_types.get(model=disco_model_name)

            if netbox_device_type:
                # Нашли точное совпадение шаблона! Применяем его к устройству
                nb_dev.device_type = netbox_device_type.id
                try:
                    nb_dev.save()
                    print(f"   ✅ Шаблон '{disco_model_name}' успешно применен! Физические порты созданы автоматически.")
                except Exception as e:
                    print(f"   ❌ Ошибка сохранения типа устройства в NetBox: {e}")
            else:
                print(f"   ⚠️  Шаблон для модели '{disco_model_name}' еще не загружен в NetBox. Пропустите.")
                print(f"       👉 Скачайте его с GitHub репозитория и импортируйте в NetBox UI.")

        except Exception as e:
            print(f"   ❌ Ошибка при обработке устройства {device_name}: {e}")

if __name__ == "__main__":
    main()