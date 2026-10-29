import os
import keyring
import pynetbox
import re
import socket
import yaml

# Загружаем переменные окружения вручную или через dotenv, если используется
from dotenv import load_dotenv
load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

# --- НАСТРОЙКИ NETBOX ---
NETBOX_URL = netbox_url
NETBOX_TOKEN = netbox_token

# --- НАСТРОЙКИ ПАПОК НА СЕРВЕРЕ ---
RAW_CONFIGS_DIR = "~/backbone/configs"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ""  # Конфиги без расширения

# Путь к локально скопированной папке с шаблонами с Гитхаба
RAW_LIBRARY_DIR = "~/device-types/Cisco"
LIBRARY_DIR = os.path.expanduser(RAW_LIBRARY_DIR)


def resolve_dns(device_name):
    try:
        return socket.gethostbyname(device_name)
    except socket.gaierror:
        return None


def extract_part_number_from_config(device_name):
    """Ищет строку PID: или Part Number внутри файла конфигурации"""
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
        # Регулярка ищет конструкции вида PID: WS-C3750G-24TS-E
        # Захватывает всё до пробела, конца строки или запятой
        match = re.search(r"PID:\s*([A-Za-z0-9-]+)", content, re.IGNORECASE)
        if match:
            return match.group(1).strip()
            
        # Запасной вариант регулярки на случай другого формата
        match_alt = re.search(r"Part\s*Number\s*:\s*([A-Za-z0-9-]+)", content, re.IGNORECASE)
        if match_alt:
            return match_alt.group(1).strip()
    except Exception as e:
        print(f"   ❌ Ошибка парсинга файла конфига {device_name}: {e}")
    return None


def find_local_template_by_pn(part_number):
    """ Сканирует локальную папку с шаблонами и ищет совпадение по part_number в YAML """
    if not os.path.exists(LIBRARY_DIR):
        print(f"🛑 Ошибка: Локальная папка с шаблонами не найдена по пути: {LIBRARY_DIR}")
        return None, None

    print(f"   🔍 Поиск локального YAML-шаблона для партномера '{part_number}'...")
    
    # Рекурсивно обходим все подпапки вендоров внутри папки device-types
    for root, dirs, files in os.walk(LIBRARY_DIR):
        for file in files:
            if file.endswith((".yaml", ".yml")):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        tpl_data = yaml.safe_load(f)
                        
                        if not tpl_data:
                            continue
                            
                        # Читаем поле part_number или имя модели из шаблона Гитхаба
                        tpl_pn = tpl_data.get("part_number") or ""
                        tpl_model = tpl_data.get("model") or ""
                        
                        # Проверяем строгое совпадение (без учета регистра)
                        if (tpl_pn.lower() == part_number.lower() or 
                            tpl_model.lower() == part_number.lower()):
                            return tpl_data, file_path
                except Exception:
                    continue
    return None, None


def get_interface_from_config(device_name, management_ip):
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            config_text = f.read()
        pattern = r"interface\s+([^\n!]+)(?:\n(?:[ \t]+[^\n]+)*\n[ \t]+ip\s+address\s+" + re.escape(management_ip) + r"\b)"
        match = re.search(pattern, config_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        lines = config_text.splitlines()
        for idx, line in enumerate(lines):
            if management_ip in line and "ip address" in line.lower():
                for i in range(idx - 1, -1, -1):
                    if lines[i].strip().lower().startswith("interface"):
                        return lines[i].replace("interface", "").strip()
    except Exception:
        pass
    return None


def get_or_create_device_type(nb, yaml_data, vendor_name):
    """Проверяет наличие типа устройства в NetBox. Если его нет — создает со всеми портами."""
    model_name = yaml_data.get("model")
    
    # Проверяем, существует ли уже этот тип в NetBox
    existing_type = nb.dcim.device_types.get(model=model_name)
    if existing_type:
        return existing_type

    print(f"   🏭 Шаблон '{model_name}' отсутствует в NetBox. Регистрируем локальный файл...")
    try:
        # Ищем или создаем Производителя (Vendor)
        slug_vendor = vendor_name.lower().replace(" ", "-").replace(".", "")
        manufacturer = nb.dcim.manufacturers.get(slug=slug_vendor)
        if not manufacturer:
            manufacturer = nb.dcim.manufacturers.create(name=vendor_name, slug=slug_vendor)

        # Создаем базовую карточку модели
        type_payload = {
            "manufacturer": manufacturer.id,
            "model": model_name,
            "slug": yaml_data.get("slug") or model_name.lower().replace(" ", "-").replace("/", "-"),
            "part_number": yaml_data.get("part_number", ""),
            "u_height": yaml_data.get("u_height", 1),
            "is_full_depth": yaml_data.get("is_full_depth", True)
        }
        new_device_type = nb.dcim.device_types.create(**type_payload)

        # Нарезаем интерфейсы из шаблона
        if "interfaces" in yaml_data and yaml_data["interfaces"]:
            for iface in yaml_data["interfaces"]:
                try:
                    nb.dcim.interface_templates.create(
                        device_type=new_device_type.id,
                        name=iface.get("name"),
                        type=iface.get("type", "1000base-t"),
                        mgmt_only=iface.get("mgmt_only", False)
                    )
                except Exception:
                    pass
        print(f"   ✅ Шаблон '{model_name}' успешно создан в NetBox со всеми портами.")
        return new_device_type
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
    print(f"\n🔄 Запуск точной синхронизации по Партномерам (PID) из конфигов: {len(nb_devices)}")

    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()
        current_role_slug = nb_dev.role.slug if nb_dev.role else ""

        # Фильтр 1: Только коммутаторы
        if current_role_slug != "switch":
            continue

        # Фильтр 2: Наличие конфига
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
        if not os.path.exists(file_path):
            continue

        print(f"\n🔎 Анализ коммутатора: {device_name}")

        # 1. Извлекаем партномер из текстового конфига
        part_number = extract_part_number_from_config(device_name)
        if not part_number:
            print(f"   ⚠️  В файле конфигурации не найдена строчка PID: или Part Number.")
            continue
        print(f"   ℹ️  Из конфига извлечен партномер (PID): '{part_number}'")

        # 2. Ищем этот партномер в локальной папке скачанных YAML файлов Гитхаба
        yaml_data, yaml_path = find_local_template_by_pn(part_number)
        if not yaml_data:
            print(f"   ❌ Шаблон для партномера '{part_number}' не найден в локальной папке {LIBRARY_DIR}")
            continue
            
        # Определяем вендора на основе имени папки (device-types/vendor_name/model.yaml)
        vendor_name = os.path.basename(os.path.dirname(yaml_path)).title()

        # 3. Гарантируем, что этот Device Type заведен в NetBox (и заведен с портами)
        target_device_type = get_or_create_device_type(nb, yaml_data, vendor_name)
        if not target_device_type:
            continue

        # Проверяем, нужно ли пересоздавать устройство
        current_interfaces = list(nb.dcim.interfaces.filter(device_id=nb_dev.id))
        interface_templates = list(nb.dcim.interface_templates.filter(device_type_id=target_device_type.id))
        
        # Пересоздаем, если тип отличается или количество портов на устройстве не совпадает с шаблоном
        need_recreate = (
            not nb_dev.device_type or 
            nb_dev.device_type.id != target_device_type.id or 
            len(current_interfaces) != len(interface_templates)
        )

        if not need_recreate:
            print(f"   🔹 Устройство уже имеет правильный тип '{target_device_type.model}' и все порты на месте. Пропускаем.")
            continue

        # Резолвим IP через DNS
        management_ip = resolve_dns(device_name)
        if not management_ip:
            print(f"   ❌ Не резолвится в DNS. Пересоздание отменено.")
            continue

        # Сохраняем метаданные для переноса
        device_payload = {
            "name": nb_dev.name,
            "device_type": target_device_type.id,
            "role": switch_role.id,
            "site": nb_dev.site.id if nb_dev.site else None,
            "status": "active",
            "tenant": nb_dev.tenant.id if nb_dev.tenant else None,
            "location": nb_dev.location.id if nb_dev.location else None,
            "rack": nb_dev.rack.id if nb_dev.rack else None,
            "position": nb_dev.position if nb_dev.position else None,
            "face": nb_dev.face.value if nb_dev.face else None,
        }

        # 4. Выполняем точечное пересоздание
        print("   🧹 Пересоздание: удаление старого объекта...")
        try:
            if nb_dev.primary_ip4:
                ip_id = nb_dev.primary_ip4.id
                nb_dev.primary_ip4 = None
                nb_dev.save()
                nb.ipam.ip_addresses.delete([ip_id])

            nb_dev.delete()
            new_dev = nb.dcim.devices.create(**device_payload)
            print("   🎉 Объект создан заново. NetBox применил фабричный шаблон портов.")
        except Exception as e:
            print(f"   ❌ Ошибка при пересоздании: {e}")
            continue

        # 5. Привязка IP на правильный порт из конфига
        target_iface_name = get_interface_from_config(device_name, management_ip) or "mgmt0"
        
        # Получаем созданный NetBox-ом из шаблона порт или досоздаем виртуальный (Vlan)
        existing_iface = nb.dcim.interfaces.get(device_id=new_dev.id, name=target_iface_name)
        if not existing_iface:
            if any(kw in target_iface_name.lower() for kw in ["vlan", "loopback", "bvi", "lo"]):
                iface_type = "virtual"
            else:
                iface_type = "1000base-t"
            target_interface = nb.dcim.interfaces.create(device=new_dev.id, name=target_iface_name, type=iface_type)
        else:
            target_interface = existing_iface

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
                
                if ip_object:
                    new_dev.primary_ip4 = ip_object.id
                    new_dev.save()
                    print(f"   ✅ IP {management_ip} привязан к интерфейсу '{target_iface_name}' и сделан Primary.")
            except Exception as e:
                print(f"   ❌ Не удалось связать IP-адрес: {e}")

    print("\n🎉 Синхронизация по локальным YAML-шаблонам партномеров успешно завершена!")

if __name__ == "__main__":
    main()
