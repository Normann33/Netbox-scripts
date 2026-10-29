import os
import keyring
import requests
import pynetbox
import io
import re
import socket
import yaml
from dotenv import load_dotenv

load_dotenv()

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

# --- НАСТРОЙКИ ФИЛЬТРАЦИИ КОНФИГОВ ---
RAW_CONFIGS_DIR = "~/backbone/configs"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ""  

# ИМЯ ЗАГЛУШКИ ДЛЯ СБРОСА
GENERIC_MODEL_NAME = "Generic Model"

# URL библиотеки devicetype-library (raw-контент)
GITHUB_LIBRARY_URL = "https://github.com"


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
    try: return socket.gethostbyname(device_name)
    except socket.gaierror: return None

def get_or_create_manufacturer(nb, vendor_name):
    slug = vendor_name.lower().replace(" ", "-").replace(".", "")
    manufacturer = nb.dcim.manufacturers.get(slug=slug)
    if manufacturer: return manufacturer
    try: return nb.dcim.manufacturers.create(name=vendor_name, slug=slug)
    except Exception: return None

def download_yaml_from_github(vendor, model):
    """
    Скачивает YAML-шаблон, используя внутренний raw-эндпоинт 
    указанного локального/корпоративного хоста GitHub
    """
    github_vendor = vendor.lower().replace(" ", "-")
    github_model = model.lower().replace(" ", "-")
    
    # Формируем прямую ссылку: https://github.com
    yaml_url = f"{GITHUB_LIBRARY_URL}/{github_vendor}/{github_model}.yaml"
    
    try:
        res = requests.get(yaml_url, verify=False)  # verify=False на случай самоподписанных SSL-сертификатов внутри сети
        if res.status_code == 200:
            return res.text
            
        # Подстраховка под расширение .yml
        yaml_url_alt = f"{GITHUB_LIBRARY_URL}/{github_vendor}/{github_model}.yml"
        res_alt = requests.get(yaml_url_alt, verify=False)
        if res_alt.status_code == 200:
            return res_alt.text
            
    except Exception as e:
        print(f"   ❌ Не удалось подключиться к репозиторию по адресу {yaml_url}: {e}")
        
    return None


def main():
    api_token = get_netdisco_session()
    if not api_token: return

    req_headers = {"Authorization": api_token}
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    
    # Проверяем наличие заглушки Generic Model
    generic_type = nb.dcim.device_types.get(model=GENERIC_MODEL_NAME)
    if not generic_type:
        print(f"🛑 Критическая ошибка: В NetBox должен существовать тип устройства '{GENERIC_MODEL_NAME}' для сброса.")
        return

    # ПОПРАВКА 1: Запрашиваем абсолютно ВСЕ устройства в системе (без фильтра по статусу active)
    all_nb_devices = list(nb.dcim.devices.all())
    
    types_to_delete = set()
    switches_to_process = []

    print(f"🛑 ЭТАП 1: Сбор типов и сброс ВСЕХ устройств в системе ({len(all_nb_devices)} шт.) в Generic...")
    for nb_dev in all_nb_devices:
        if not nb_dev.name: continue
        device_name = nb_dev.name.strip()
        current_role_slug = nb_dev.role.slug if nb_dev.role else ""
        current_status = nb_dev.status.value if nb_dev.status else ""

        # Для последующего импорта и пересоздания отбираем только свитчи с конфигами
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
        has_config = os.path.exists(file_path)
        
        if current_role_slug == "switch" and has_config and current_status == "active":
            switches_to_process.append(nb_dev)

        # Но сбрасываем тип устройства БЕЗУСЛОВНО для всех, кто держит удаляемые шаблоны
        if nb_dev.device_type and nb_dev.device_type.model != GENERIC_MODEL_NAME:
            types_to_delete.add(nb_dev.device_type.id)
            
            print(f"  🔄 Временный сброс типа для [{current_status.upper()}] {device_name} -> '{GENERIC_MODEL_NAME}'")
            nb_dev.device_type = generic_type.id
            try:
                nb_dev.save()
            except Exception as e:
                print(f"    ❌ Не удалось сбросить тип для {device_name}: {e}")

    print(f"\n🗑️ ЭТАП 2: Тотальное удаление пустых/некорректных типов из NetBox ({len(types_to_delete)} шт.)...")
    for type_id in types_to_delete:
        try:
            t = nb.dcim.device_types.get(id=type_id)
            if t:
                print(f"  ❌ Удаляем тип: '{t.model}'")
                t.delete()
        except Exception as e:
            print(f"    ⚠️ Зависимость не разорвана для типа ID {type_id}: {e}")

    print("\n🚀 ЭТАП 3: Повторный чистый импорт шаблонов портов на основе JSON Netdisco...")
    
    unique_models_imported = {}

    for nb_dev in switches_to_process:
        device_name = nb_dev.name.strip()
        
        management_ip = nb_dev.primary_ip4.address.split('/') if nb_dev.primary_ip4 else resolve_dns(device_name)
        if not management_ip: continue

        # ПОПРАВКА 2: Парсим JSON напрямую, ключи vendor и model с маленькой буквы
        disco_model_name = None
        vendor_name = None
        try:
            res = requests.get(f"{NETDISCO_URL}/api/v1/object/device/{management_ip[0]}", headers=req_headers)
            if res.status_code == 200:
                # Netdisco возвращает JSON-массив или объект. Забираем его
                disco_data = res.json()
                if isinstance(disco_data, list) and len(disco_data) > 0:
                    device_info = disco_data[0]
                elif isinstance(disco_data, dict):
                    device_info = disco_data
                else:
                    continue
                
                vendor_name = device_info.get("vendor", "").strip()
                disco_model_name = device_info.get("model", "").strip()
        except Exception as e:
            print(f"❌ Ошибка парсинга JSON из Netdisco для {device_name}: {e}")
            continue

        if not disco_model_name or not vendor_name:
            print(f"❌ {device_name}: Нет точных данных по модели/вендору в JSON ответа.")
            continue

        # Если этот шаблон уже залит на этом шаге
        if disco_model_name in unique_models_imported:
            nb_dev.device_type = unique_models_imported[disco_model_name]
            nb_dev.save()
            continue

        print(f"\n📥 Скачивание шаблона '{disco_model_name}' с GitHub...")
        manufacturer = get_or_create_manufacturer(nb, vendor_name)
        if not manufacturer: continue

        yaml_content = download_yaml_from_github(vendor_name, disco_model_name)
        if not yaml_content:
            print(f"   ❌ Шаблон для '{disco_model_name}' не найден в репозитории devicetype-library.")
            continue

        try:
            parsed_yaml = yaml.safe_load(yaml_content)
            
            device_type_data = {
                "manufacturer": manufacturer.id,
                "model": parsed_yaml.get("model", disco_model_name),
                "slug": parsed_yaml.get("slug") or disco_model_name.lower().replace(" ", "-").replace("/", "-"),
                "part_number": parsed_yaml.get("part_number", ""),
                "u_height": parsed_yaml.get("u_height", 1),
                "is_full_depth": parsed_yaml.get("is_full_depth", True)
            }
            
            new_device_type = nb.dcim.device_types.create(**device_type_data)
            unique_models_imported[disco_model_name] = new_device_type.id
            
            # Нарезаем интерфейсы
            interfaces_count = 0
            if "interfaces" in parsed_yaml and parsed_yaml["interfaces"]:
                for iface in parsed_yaml["interfaces"]:
                    try:
                        nb.dcim.interface_templates.create(
                            device_type=new_device_type.id,
                            name=iface.get("name"),
                            type=iface.get("type", "1000base-t"),
                            mgmt_only=iface.get("mgmt_only", False)
                        )
                        interfaces_count += 1
                    except Exception:
                        pass
            
            print(f"   ✅ Шаблон '{disco_model_name}' успешно создан с компонентами! Интерфейсов: {interfaces_count}")
            
            nb_dev.device_type = new_device_type.id
            nb_dev.save()
            print(f"   🔗 Устройство {device_name} привязано к свежему заводскому шаблону.")

        except Exception as e:
            print(f"   ❌ Не удалось импортировать компоненты для {disco_model_name}: {e}")

    print("\n🎉 Тотальный перезапуск структуры шаблонов успешно завершен!")

if __name__ == "__main__":
    main()
