import os
import keyring
import requests
import pynetbox
import csv
import io
import re
import socket  # Модуль для работы с DNS
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
CONFIG_EXTENSION = ""  

# --- ДНС СУФФИКС (Опционально) ---
# Если ваши устройства в DNS записаны с доменом (например, switch-3.domain.local), 
# а в NetBox они называются просто "switch-3", укажите ваш домен ниже.
# Если имена в NetBox и DNS совпадают 1 в 1, оставьте пустую строку "".
DNS_SUFFIX = "" 


def get_netdisco_session():
    """Авторизуется в Netdisco по логину/паролю и возвращает чистый API-токен"""
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
    """Пытается разрешить имя устройства через DNS в IP-адрес"""
    hostname = device_name
    if DNS_SUFFIX and not hostname.endswith(DNS_SUFFIX):
        hostname = f"{device_name}.{DNS_SUFFIX.lstrip('.')}"
        
    try:
        ip_address = socket.gethostbyname(hostname)
        return ip_address
    except socket.gaierror:
        # Если не отрезолвилось по полному имени, пробуем по "голому" имени из NetBox
        if hostname != device_name:
            try:
                return socket.gethostbyname(device_name)
            except socket.gaierror:
                pass
        return None


def get_interface_from_config(device_name, management_ip):
    """Открывает файл конфигурации и ищет, на каком интерфейсе прописан IP"""
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    
    if not os.path.exists(file_path):
        print(f"   ⚠️  Файл конфигурации не найден по пути: {file_path}")
        return None

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            config_text = f.read()

        pattern = r"interface\s+([^\n!]+)(?:\n(?:[ \t]+[^\n]+)*\n[ \t]+ip\s+address\s+" + re.escape(management_ip) + r"\b)"
        match = re.search(pattern, config_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # Запасной вариант (построчный поиск "назад")
        lines = config_text.splitlines()
        for idx, line in enumerate(lines):
            if management_ip in line and "ip address" in line.lower():
                for i in range(idx - 1, -1, -1):
                    if lines[i].strip().lower().startswith("interface"):
                        return lines[i].replace("interface", "").strip()

    except Exception as e:
        print(f"   ❌ Ошибка при чтении файла конфигурации {file_path}: {e}")
        
    return None


def get_or_create_interface(nb, nb_dev, interface_name):
    """Ищет интерфейс по имени в NetBox, если не находит — создает его с правильным типом"""
    existing_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=interface_name)
    if existing_iface:
        return existing_iface
        
    print(f"   ⚠️  Интерфейс '{interface_name}' не найден в NetBox. Создаем...")
    try:
        name_lower = interface_name.lower()
        if any(kw in name_lower for kw in ["vlan", "loopback", "bvi", "lo", "port-channel", "po"]):
            iface_type = "virtual"
        elif "ten" in name_lower or "tg" in name_lower:
            iface_type = "10base-x-sfpp"       
        elif "gig" in name_lower or "ge" in name_lower:
            iface_type = "1000base-t"          
        elif "fast" in name_lower or "fa" in name_lower:
            iface_type = "100base-tx"          
        else:
            iface_type = "1000base-t"
        
        new_iface = nb.dcim.interfaces.create(
            device=nb_dev.id,
            name=interface_name,
            type=iface_type
        )
        print(f"   ✅ Создан интерфейс '{interface_name}' с типом '{iface_type}'")
        return new_iface
    except Exception as e:
        print(f"   ❌ Не удалось создать интерфейс '{interface_name}': {e}")
        return None


def main():
    # 1. Авторизуемся в Netdisco (нужно только для инвентаря: серийники и ОС)
    api_token = get_netdisco_session()
    if not api_token:
        print("🛑 Синхронизация отменена из-за ошибки авторизации в Netdisco.")
        return

    req_headers = {"Authorization": api_token}
    print("✅ Авторизация в Netdisco успешна. Токен получен.")

    # 2. Инициализируем NetBox
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    nb_devices = nb.dcim.devices.filter(status="active")
    
    print("\n🔄 Начинаем точную сверку устройств через DNS и конфиги...")
    
    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()

        # Если Primary IP уже есть — полностью пропускаем устройство
        if nb_dev.primary_ip4:
            print(f"🔹 {device_name}: уже имеет Primary IP. Пропускаем.")
            continue
            
        # Проверяем, есть ли вообще конфиг для этого устройства, прежде чем тратить время на DNS и API
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
        if not os.path.exists(file_path):
            print(f"⏭️  {device_name}: файл конфигурации не найден ({file_path}). Пропускаем.")
            continue

        # 3. РЕЗОЛВИМ MANAGEMENT IP ЧЕРЕЗ DNS
        management_ip = resolve_dns(device_name)
        if not management_ip:
            print(f"❌ {device_name}: Не удалось разрешить имя через DNS. Пропускаем.")
            continue
            
        print(f"🔎 {device_name} -> Успешно резолвился в IP: {management_ip}")

        # 4. ПОЛУЧАЕМ ИНВЕНТАРНЫЕ ДАННЫЕ ИЗ NETDISCO ПО ТОЧНОМУ IP
        # Больше никаких нестрогих поисков по имени устройства! Спрашиваем строго по DNS IP
        real_serial = ""
        real_os_version = ""
        
        device_res = requests.get(f"{NETDISCO_URL}/api/v1/object/device/{management_ip}", headers=req_headers)
        if device_res.status_code == 200:
            csv_text = device_res.content.decode('utf-8')
            reader = csv.DictReader(io.StringIO(csv_text.strip()))
            search_data = list(reader)
            if search_data:
                disco_device = search_data[0]
                real_serial = disco_device.get("Serials", "").strip()
                real_os_version = disco_device.get("OS Version", "").strip()

        has_changes = False
        
        # Обновляем серийный номер
        if real_serial and nb_dev.serial != real_serial:
            print(f"   🔎 Найдено расхождение серийного номера: '{nb_dev.serial}' -> '{real_serial}'")
            nb_dev.serial = real_serial
            has_changes = True
            
        # Обновляем ОС
        if hasattr(nb_dev, 'custom_fields') and 'os_version' in nb_dev.custom_fields:
            if nb_dev.custom_fields['os_version'] != real_os_version and real_os_version:
                print(f"   🔎 Найдено расхождение версии ОС: '{real_os_version}'")
                nb_dev.custom_fields['os_version'] = real_os_version
                has_changes = True

        if has_changes:
            try:
                nb_dev.save()
            except Exception as e:
                print(f"   ❌ Ошибка сохранения базовых данных: {e}")

        # 5. НАПОЛНЕНИЕ IP И ИНТЕРФЕЙСОВ ИЗ КОНФИГА
        # Ищем интерфейс в локальном конфиге по нашему DNS IP
        target_iface_name = get_interface_from_config(device_name, management_ip)
        
        if not target_iface_name:
            print(f"   ⚠️  IP {management_ip} найден в DNS, но не обнаружен внутри {file_path}. Подстраховка: 'mgmt0'")
            target_iface_name = "mgmt0"
        else:
            print(f"   ℹ️  Интерфейс успешно определен из файла конфигурации: '{target_iface_name}'")

        ip_object = nb.ipam.ip_addresses.get(address=management_ip)
        target_interface = get_or_create_interface(nb, nb_dev, target_iface_name)
        
        if target_interface:
            if ip_object:
                ip_object.assigned_object_type = "dcim.interface"
                ip_object.assigned_object_id = target_interface.id
                ip_object.save()
            else:
                try:
                    ip_object = nb.ipam.ip_addresses.create(
                        address=f"{management_ip}/32",
                        assigned_object_type="dcim.interface",
                        assigned_object_id=target_interface.id
                    )
                except Exception as e:
                    print(f"   ❌ Не удалось создать IP-адрес {management_ip}: {e}")
                    ip_object = None

        if ip_object:
            try:
                nb_dev.primary_ip4 = ip_object.id
                nb_dev.save()
                print(f"   ✅ IP {management_ip} успешно назначен как Primary IPv4 на интерфейс '{target_iface_name}'.")
            except Exception as e:
                print(f"   ❌ Ошибка при назначении Primary IP для {nb_dev.name}: {e}")
                
if __name__ == "__main__":
    main()