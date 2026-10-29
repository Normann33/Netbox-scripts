import os
import keyring
import requests
import pynetbox
import csv
import io
import socket
import yaml
from dotenv import load_dotenv

load_dotenv()

# Авторизация в системах
netdisco_url = os.getenv("NETDISCO_URL")
netdisco_username = os.getenv("NETDISCO_USER")
netdisco_password = keyring.get_password("Netdisco", netdisco_username)

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

NETBOX_URL = netbox_url
NETBOX_TOKEN = netbox_token
NETDISCO_URL = netdisco_url
NETDISCO_USER = netdisco_username       
NETDISCO_PASS = netdisco_password       

# --- НАСТРОЙКИ ПАПКИ С КОНФИГАМИ ---
RAW_CONFIGS_DIR = "~/backbone/configs"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ".cfg"  

# URL официальной библиотеки шаблонов NetBox на GitHub (raw-файлы)
GITHUB_LIBRARY_URL = "https://githubusercontent.com"


def get_netdisco_session():
    login_url = f"{NETDISCO_URL}/login"
    try:
        response = requests.post(login_url, auth=(NETDISCO_USER, NETDISCO_PASS), headers={'Accept': 'application/json'})
        if response.status_code == 200:
            try: return response.json().get("api_key")
            except ValueError: return response.text.strip()
        return None
    except requests.exceptions.RequestException:
        return None


def resolve_dns(device_name):
    try:
        return socket.gethostbyname(device_name)
    except socket.gaierror:
        return None


def get_or_create_manufacturer(nb, vendor_name):
    slug = vendor_name.lower().replace(" ", "-").replace(".", "")
    manufacturer = nb.dcim.manufacturers.get(slug=slug)
    if manufacturer:
        return manufacturer
        
    print(f"   🏭 Производитель '{vendor_name}' не найден в NetBox. Создаем...")
    try:
        return nb.dcim.manufacturers.create(name=vendor_name, slug=slug)
    except Exception as e:
        print(f"   ❌ Не удалось создать производителя '{vendor_name}': {e}")
        return None


def download_yaml_from_github(vendor, model):
    github_vendor = vendor.lower().replace(" ", "-")
    github_model = model.lower().replace(" ", "-")
    
    yaml_url = f"{GITHUB_LIBRARY_URL}/{github_vendor}/{github_model}.yaml"
    res = requests.get(yaml_url)
    if res.status_code == 200:
        return res.text
        
    yaml_url_alt = f"{GITHUB_LIBRARY_URL}/{github_vendor}/{github_model}.yml"
    res_alt = requests.get(yaml_url_alt)
    if res_alt.status_code == 200:
        return res_alt.text
        
    return None


def main():
    # 1. Авторизация
    api_token = get_netdisco_session()
    if not api_token:
        print("🛑 Ошибка авторизации в Netdisco.")
        return

    req_headers = {"Authorization": api_token}
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    
    nb_devices = nb.dcim.devices.filter(status="active")
    unique_models_to_fetch = {}
    
    print("\n🔍 Анализируем сеть и ищем используемые модели устройств...")
    
    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()
        
        # Проверяем, есть ли локальный конфиг
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}")
        if not os.path.exists(file_path):
            print(f"Skip: {device_name} (конфиг не найден)")
            continue
            
        # Резолвим IP
        management_ip = nb_dev.primary_ip4.address.split('/') if nb_dev.primary_ip4 else resolve_dns(device_name)
        if not management_ip:
            print(f"❌ {device_name}: Не удалось разрешить имя через DNS. Пропускаем.")
            continue
            
        # Запрашиваем информацию из Netdisco
        try:
            res = requests.get(f"{NETDISCO_URL}/api/v1/object/device/{management_ip}", headers=req_headers)
            if res.status_code == 200:
                csv_text = res.content.decode('utf-8')
                reader = csv.DictReader(io.StringIO(csv_text.strip()))
                data = list(reader)
                if data:
                    disco_device = data
                    vendor = disco_device.get("Vendor", "").strip()
                    model = disco_device.get("Model", "").strip()
                    
                    if vendor and model:
                        # Проверяем, существует ли уже такой тип устройства в самом NetBox
                        exists_in_netbox = nb.dcim.device_types.get(model=model)
                        
                        if exists_in_netbox:
                            # ДОБАВЛЕНО: Информируем, если шаблон уже есть в NetBox
                            print(f"🔹 Модель '{model}' уже существует в NetBox. Пропускаем.")
                        else:
                            unique_models_to_fetch[model] = vendor
        except Exception:
            continue

    if not unique_models_to_fetch:
        print("\n🔹 Новых моделей для импорта не найдено.")
        return

    print(f"\n📋 Найдено новых уникальных моделей для импорта с GitHub: {len(unique_models_to_fetch)}")

    # 2. Скачивание и импорт шаблонов
    for model_name, vendor_name in unique_models_to_fetch.items():
        print(f"\n🚀 Обработка модели '{model_name}' ({vendor_name})...")
        
        manufacturer = get_or_create_manufacturer(nb, vendor_name)
        if not manufacturer:
            continue

        yaml_content = download_yaml_from_github(vendor_name, model_name)
        if not yaml_content:
            print(f"   ❌ Шаблон для '{model_name}' не найден на GitHub.")
            continue
            
        try:
            parsed_yaml = yaml.safe_load(yaml_content)
            
            device_type_data = {
                "manufacturer": manufacturer.id,
                "model": parsed_yaml.get("model", model_name),
                "slug": parsed_yaml.get("slug") or model_name.lower().replace(" ", "-").replace("/", "-"),
                "part_number": parsed_yaml.get("part_number", ""),
                "u_height": parsed_yaml.get("u_height", 1),
                "is_full_depth": parsed_yaml.get("is_full_depth", True)
            }
            
            nb.dcim.device_types.create(**device_type_data)
            # ДОБАВЛЕНО: Сообщение об успешной заливке шаблона
            print(f"   ✅ Шаблон устройства '{model_name}' успешно скачан и зарегистрирован в NetBox!")
            
        except Exception as e:
            print(f"   ❌ Ошибка при импорте шаблона для {model_name} в NetBox: {e}")

    print("\n🎉 Процесс автоматического импорта шаблонов завершен!")

if __name__ == "__main__":
    main()