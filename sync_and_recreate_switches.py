import os
import keyring
import pynetbox
import re
import socket
import yaml
from dotenv import load_dotenv

load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

# --- НАСТРОЙКИ NETBOX ---
NETBOX_URL = netbox_url
NETBOX_TOKEN = netbox_token

# --- НАСТРОЙКИ ВНУТРЕННИХ ПАПОК ---
RAW_CONFIGS_DIR = "~/backbone/configs"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ""  # Конфиги без расширения

# Путь к локальной копии папки device-types из репозитория
RAW_LIBRARY_DIR = "~/backbone/device-types"
LIBRARY_DIR = os.path.expanduser(RAW_LIBRARY_DIR)

# Читаем домен поиска из .env
DNS_SUFFIX = os.getenv("DNS_SUFFIX", "").strip()


def resolve_dns(device_name):
    """Резолвит имя устройства через DNS в IP-адрес с учетом суффикса"""
    hostname = device_name
    if DNS_SUFFIX and not hostname.endswith(DNS_SUFFIX):
        hostname = f"{device_name}.{DNS_SUFFIX.lstrip('.')}"
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        if hostname != device_name:
            try: return socket.gethostbyname(device_name)
            except socket.gaierror: pass
        return None


def extract_part_number_from_config(device_name):
    """Ищет партномер (PID) внутри файла конфигурации устройства"""
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        match = re.search(r"PID:\s*([A-Za-z0-9-]+)", content, re.IGNORECASE)
        if match: return match.group(1).strip()
        match_alt = re.search(r"Part\s*Number\s*:\s*([A-Za-z0-9-]+)", content, re.IGNORECASE)
        if match_alt: return match_alt.group(1).strip()
    except Exception:
        pass
    return None


def find_local_template_by_pn(part_number):
    """Сканирует локальную библиотеку и ищет совпадение по part_number в YAML"""
    if not os.path.exists(LIBRARY_DIR):
        return None, None
    for root, dirs, files in os.walk(LIBRARY_DIR):
        for file in files:
            if file.endswith((".yaml", ".yml")):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        tpl_data = yaml.safe_load(f)
                        if tpl_data:
                            tpl_pn = tpl_data.get("part_number") or ""
                            tpl_model = tpl_data.get("model") or ""
                            if tpl_pn.lower() == part_number.lower() or tpl_model.lower() == part_number.lower():
                                return tpl_data, file_path
                except Exception: continue
    return None, None


def get_all_interfaces_from_config(device_name):
    """
    Парсит весь файл конфигурации и извлекает список абсолютно ВСЕХ 
    объявленных интерфейсов устройства.
    """
    interfaces = set()
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    if not os.path.exists(file_path):
        return interfaces

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_strip = line.strip()
                # Ищем строки, начинающиеся с interface, отсекая sub-интерфейсы по желанию или собирая всё
                if line_strip.lower().startswith("interface ") and not line_strip.endswith(r" description"):
                    iface_name = line_strip.replace("interface", "", 1).strip()
                    if iface_name and not iface_name.lower().startswith("bgb"):
                        interfaces.add(iface_name)
    except Exception as e:
        print(f"   ❌ Ошибка чтения интерфейсов из конфига {device_name}: {e}")
    return interfaces


def find_mgmt_interface_from_config(device_name, management_ip):
    """Определяет по файлу конфигурации, на каком именно порту висит management_ip"""
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            config_text = f.read()
        pattern = r"interface\s+([^\n!]+)(?:\n(?:[ \t]+[^\n]+)*\n[ \t]+ip\s+address\s+" + re.escape(management_ip) + r"\b)"
        match = re.search(pattern, config_text, re.IGNORECASE)
        if match: return match.group(1).strip()
            
        lines = config_text.splitlines()
        for idx, line in enumerate(lines):
            if management_ip in line and "ip address" in line.lower():
                for i in range(idx - 1, -1, -1):
                    if lines[i].strip().lower().startswith("interface"):
                        return lines[i].replace("interface", "").strip()
    except Exception:
        pass
    return None


def get_or_create_interface(nb, nb_dev, interface_name):
    """Ищет интерфейс в NetBox, если его нет — создает со строгим определением типа"""
    existing_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=interface_name)
    if existing_iface:
        return existing_iface
        
    try:
        name_lower = interface_name.lower().strip()
        
        # 1. СТРОГАЯ ПРОВЕРКА НА ЛОГИЧЕСКИЕ И SUB-ИНТЕРФЕЙСЫ (NetBox 4.x требует 'virtual')
        # Если в имени есть точка (н-р Gi0/1.421) или это VLAN/Loopback/Port-Channel
        if ("." in name_lower or 
            any(kw in name_lower for kw in ["vlan", "loopback", "bvi", "lo", "port-channel", "po"])):
            iface_type = "virtual"
            
        # 2. ПРОВЕРКА ФИЗИЧЕСКИХ ПОРТОВ (Ищем строго по началу строки)
        elif name_lower.startswith(("tengigabitethernet", "ten", "te", "tg")):
            iface_type = "10base-x-sfpp"  # 10 Гбит/с SFP+
        elif name_lower.startswith(("gigabitethernet", "gig", "gi", "g")):
            iface_type = "1000base-t"     # 1 Гбит/с Copper
        elif name_lower.startswith(("fastethernet", "fast", "fa", "f")):
            iface_type = "100base-tx"     # 100 Мбит/с FastEthernet
        elif name_lower.startswith(("ethernet", "eth", "et", "e")):
            iface_type = "1000base-t"     # Дефолтный физический Ethernet
        else:
            iface_type = "1000base-t"     # Универсальный подстраховочный дефолт

        new_iface = nb.dcim.interfaces.create(
            device=nb_dev.id,
            name=interface_name,
            type=iface_type
        )
        return new_iface
    except Exception as e:
        print(f"   ❌ Не удалось создать интерфейс '{interface_name}' с типом '{iface_type}': {e}")
        return None


def get_or_create_device_type(nb, yaml_data, vendor_name):
    model_name = yaml_data.get("model")
    existing_type = nb.dcim.device_types.get(model=model_name)
    if existing_type: return existing_type

    print(f"   🏭 Шаблон '{model_name}' отсутствует в NetBox. Регистрируем...")
    try:
        slug_vendor = vendor_name.lower().replace(" ", "-").replace(".", "")
        manufacturer = nb.dcim.manufacturers.get(slug=slug_vendor) or nb.dcim.manufacturers.create(name=vendor_name, slug=slug_vendor)

        new_type = nb.dcim.device_types.create(
            manufacturer=manufacturer.id, model=model_name,
            slug=yaml_data.get("slug") or model_name.lower().replace(" ", "-").replace("/", "-"),
            part_number=yaml_data.get("part_number", "")
        )
        if "interfaces" in yaml_data and yaml_data["interfaces"]:
            for iface in yaml_data["interfaces"]:
                try:
                    nb.dcim.interface_templates.create(device_type=new_type.id, name=iface.get("name"), type=iface.get("type", "1000base-t"))
                except Exception: pass
        return new_type
    except Exception as e:
        print(f"   ❌ Ошибка при импорте типа устройства: {e}")
        return None


def main():
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    switch_role = nb.dcim.device_roles.get(slug="switch")
    if not switch_role:
        print("🛑 Критическая ошибка: Роль со slug 'switch' отсутствует в NetBox.")
        return

    nb_devices = list(nb.dcim.devices.filter(status="active"))
    print(f"\n🔄 Синхронизация фабричных панелей портов и кастомных интерфейсов из файлов конфигураций: {len(nb_devices)}")

    for nb_dev in nb_devices:
        if not nb_dev.name: continue
        device_name = nb_dev.name.strip()
        current_role_slug = nb_dev.role.slug if nb_dev.role else ""

        if current_role_slug != "switch": continue
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
        if not os.path.exists(file_path): continue

        print(f"\n🔎 Анализ коммутатора: {device_name}")

        # 1. Извлекаем партномер и ищем шаблон
        part_number = extract_part_number_from_config(device_name)
        target_device_type = None
        if part_number:
            yaml_data, yaml_path = find_local_template_by_pn(part_number)
            if yaml_data:
                vendor_name = os.path.basename(os.path.dirname(yaml_path)).title()
                target_device_type = get_or_create_device_type(nb, yaml_data, vendor_name)

        # 2. Сверяем порты для триггера пересоздания
        current_interfaces = list(nb.dcim.interfaces.filter(device_id=nb_dev.id))
        templated_ports_count = 0
        if target_device_type:
            interface_templates = list(nb.dcim.interface_templates.filter(device_type_id=target_device_type.id))
            templated_ports_count = len(interface_templates)
        elif nb_dev.device_type:
            interface_templates = list(nb.dcim.interface_templates.filter(device_type_id=nb_dev.device_type.id))
            templated_ports_count = len(interface_templates)

        need_recreate = (
            (target_device_type and (not nb_dev.device_type or nb_dev.device_type.id != target_device_type.id)) or 
            len(current_interfaces) != templated_ports_count or 
            templated_ports_count == 0
        )

        active_dev = nb_dev
        if need_recreate and target_device_type:
            # Выполняем пересоздание
            management_ip = resolve_dns(device_name)
            if not management_ip:
                print(f"   ❌ Не резолвится в DNS. Пересоздание пропущено.")
                continue

            device_payload = {
                "name": nb_dev.name, "device_type": target_device_type.id, "role": switch_role.id,
                "site": nb_dev.site.id if nb_dev.site else None, 
                "status": "active",
                "tenant": nb_dev.tenant.id if nb_dev.tenant else None, 
                "location": nb_dev.location.id if nb_dev.location else None,
                "rack": nb_dev.rack.id if nb_dev.rack else None, 
                "position": nb_dev.position if nb_dev.position else None,
                "face": nb_dev.face.value if nb_dev.face else None,
            }
            print("   🧹 Пересоздание: удаление старого объекта...")
            try:
                if nb_dev.primary_ip4:
                    ip_id = nb_dev.primary_ip4.id
                    nb_dev.primary_ip4 = None
                    nb_dev.save()
                    nb.ipam.ip_addresses.delete([ip_id])
                nb_dev.delete()
                active_dev = nb.dcim.devices.create(**device_payload)
                print(f"   🎉 Объект пересоздан на основе фабричного шаблона '{target_device_type.model}'.")
            except Exception as e:
                print(f"   ❌ Ошибка при пересоздании: {e}")
                continue
        else:
            print(f"   🔹 Базовая карточка '{active_dev.device_type.model}' корректна. Переходим к донарезке портов.")

        # 3. ТОТАЛЬНАЯ ДОНАРЕЗКА ИНТЕРФЕЙСОВ ИЗ КОНФИГА
        config_interfaces = get_all_interfaces_from_config(device_name)
        print(f"   📊 Найдено интерфейсов в файле конфигурации: {len(config_interfaces)}")
        
        # Получаем текущее количество портов перед началом досоздания
        pre_count = len(list(nb.dcim.interfaces.filter(device_id=active_dev.id)))
        
        for iface_name in config_interfaces:
            get_or_create_interface(nb, active_dev, iface_name)
            
        # Считаем разницу, чтобы понять, сколько портов реально досоздалось
        post_count = len(list(nb.dcim.interfaces.filter(device_id=active_dev.id)))
        created_count = post_count - pre_count

        if created_count > 0:
            print(f"   ✅ Из файла конфигурации досоздано уникальных интерфейсов: {created_count}")

        # 4. Привязка Management IP
        management_ip = resolve_dns(device_name)
        if management_ip:
            target_iface_name = find_mgmt_interface_from_config(device_name, management_ip) or "mgmt0"
            target_interface = get_or_create_interface(nb, active_dev, target_iface_name)
            
            if target_interface:
                try:
                    ip_object = nb.ipam.ip_addresses.get(address=management_ip)
                    if ip_object:
                        ip_object.assigned_object_type = "dcim.interface"
                        ip_object.assigned_object_id = target_interface.id
                        ip_object.save()
                    else:
                        ip_object = nb.ipam.ip_addresses.create(
                            address=f"{management_ip}/32", 
                            assigned_object_type="dcim.interface", 
                            assigned_object_id=target_interface.id
                        )
                        
                    if ip_object and (not active_dev.primary_ip4 or active_dev.primary_ip4.address.split('/')[0] != management_ip):
                        active_dev.primary_ip4 = ip_object.id
                        active_dev.save()
                        print(f"   ✅ IP {management_ip} жестко закреплен за интерфейсом '{target_iface_name}' и сделан Primary.")
                except Exception as e:
                    print(f"   ❌ Не удалось связать IP-адрес: {e}")

    print("\n🎉 Все коммутаторы успешно насыщены фабричными и кастомными интерфейсами!")

if __name__ == "__main__":
    main()
