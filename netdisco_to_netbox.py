import os
import keyring
import requests
import pynetbox
import csv
import io
import re
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
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  # Путь к папке, где лежат конфиги устройств
# CONFIG_EXTENSION = ""  # Расширение файлов конфигов (.cfg, .txt, .conf)


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


def get_interface_from_config(device_name, management_ip):
    """
    Открывает файл конфигурации устройства и ищет, 
    на каком интерфейсе прописан указанный management_ip
    """
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}")
    
    if not os.path.exists(file_path):
        print(f"   ⚠️  Файл конфигурации не найден по пути: {file_path}")
        return None

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            config_text = f.read()

        # Регулярное выражение для Cisco-подобных конфигов (работает для Cisco, Huawei, Eltex, Arista)
        # Оно ищет блок "interface <имя>", внутри которого (до следующего интерфейса или восклицательного знака)
        # содержится наш целевой IP-адрес.
        pattern = r"interface\s+([^\n!]+)(?:\n(?:[ \t]+[^\n]+)*\n[ \t]+ip\s+address\s+" + re.escape(management_ip) + r"\b)"
        
        match = re.search(pattern, config_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # Запасной вариант (построчный поиск "назад"): если регулярка не сработала из-за форматирования
        lines = config_text.splitlines()
        for idx, line in enumerate(lines):
            if management_ip in line and "ip address" in line.lower():
                # Двигаемся вверх от строки с IP-адресом в поисках объявления интерфейса
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
        
        # Автоопределение типа для NetBox 4.x
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
    # 1. Авторизуемся в Netdisco
    api_token = get_netdisco_session()
    if not api_token:
        print("🛑 Синхронизация отменена из-за ошибки авторизации в Netdisco.")
        return

    req_headers = {"Authorization": api_token}
    print("✅ Авторизация в Netdisco успешна. Токен получен.")

    # 2. Инициализируем NetBox
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    nb_devices = nb.dcim.devices.filter(status="active")
    
    print("\n🔄 Начинаем точную сверку устройств...")
    
    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()

        # Если Primary IP уже есть — полностью пропускаем устройство
        if nb_dev.primary_ip4:
            print(f"🔹 {device_name}: уже имеет Primary IP. Пропускаем.")
            continue
        
        # 3. Запрос к Netdisco для поиска устройства
        search_res = requests.get(
            f"{NETDISCO_URL}/api/v1/search/device", 
            headers=req_headers,
            params={"q": device_name}
        )
        
        if search_res.status_code != 200:
            print(f"⚠️ Ошибка поиска для {device_name}. Статус: {search_res.status_code}")
            continue

        csv_text = search_res.content.decode('utf-8')
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        search_data = list(reader)
        
        if not search_data:
            print(f"❓ Устройство '{device_name}' не найдено в базе Netdisco.")
            continue
            
        # СТРОГАЯ ВАЛИДАЦИЯ ИМЕН (Разделяем switch-3 и switch-3-3)
        disco_device = None
        for dev in search_data:
            sys_name = dev.get("System Name", "").strip()
            if sys_name.lower() == device_name.lower() or sys_name.lower().startswith(device_name.lower() + "."):
                disco_device = dev
                break
        
        if not disco_device:
            print(f"❓ Точное совпадение для устройства '{device_name}' не найдено в выдаче Netdisco.")
            continue
            
        real_serial = disco_device.get("Serials", "").strip()
        real_os_version = disco_device.get("OS Version", "").strip()
        management_ip = disco_device.get("Management IP", "").strip()
        
        has_changes = False
        
        # Обновляем серийник
        if real_serial and nb_dev.serial != real_serial:
            print(f"🔎 Найдено расхождение серийного номера для {nb_dev.name}: '{nb_dev.serial}' -> '{real_serial}'")
            nb_dev.serial = real_serial
            has_changes = True
            
        # Обновляем ОС
        if hasattr(nb_dev, 'custom_fields') and 'os_version' in nb_dev.custom_fields:
            if nb_dev.custom_fields['os_version'] != real_os_version:
                print(f"🔎 Найдено расхождение версии ОС для {nb_dev.name}: '{real_os_version}'")
                nb_dev.custom_fields['os_version'] = real_os_version
                has_changes = True

        if has_changes:
            try:
                nb_dev.save()
                print(f"   ✅ Базовые данные для {nb_dev.name} успешно обновлены.")
            except Exception as e:
                print(f"   ❌ Ошибка сохранения базовых данных {nb_dev.name}: {e}")

        # 4. НАПОЛНЕНИЕ IP И ИНТЕРФЕЙСОВ ИЗ КОНФИГА
        if management_ip:
            print(f"🚀 Заполнение сетевых данных для {nb_dev.name} -> IP: {management_ip}")
            
            # ИЩЕМ ИНТЕРФЕЙС В ЛОКАЛЬНОМ КОНФИГЕ ФАЙЛА
            target_iface_name = get_interface_from_config(device_name, management_ip)
            
            if not target_iface_name:
                print(f"   ⚠️  Не удалось определить интерфейс из конфига. Подстраховка: 'mgmt0'")
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
                    print(f"   ✅ IP {management_ip} успешно назначен как Primary IPv4.")
                except Exception as e:
                    print(f"   ❌ Ошибка при назначении Primary IP для {nb_dev.name}: {e}")

if __name__ == "__main__":
    main()
