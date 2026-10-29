import os
import keyring
import requests
import pynetbox
import csv
import io
import re
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
CONFIG_EXTENSION = ""  # Конфиги без расширения


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


def get_interface_from_config(device_name, management_ip):
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    if not os.path.exists(file_path):
        return None
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


def get_or_create_interface(nb, nb_dev, interface_name):
    """Ищет интерфейс по имени. Если его нет (например, виртуальный VLAN), создает его."""
    existing_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=interface_name)
    if existing_iface:
        return existing_iface
        
    print(f"   ⚠️  Интерфейс '{interface_name}' не найден в заводском шаблоне. Создаем вручную...")
    try:
        name_lower = interface_name.lower()
        if any(kw in name_lower for kw in ["vlan", "loopback", "bvi", "lo", "port-channel", "po"]):
            iface_type = "virtual"
        else:
            iface_type = "1000base-t"
        
        return nb.dcim.interfaces.create(device=nb_dev.id, name=interface_name, type=iface_type)
    except Exception as e:
        print(f"   ❌ Не удалось досоздать интерфейс '{interface_name}': {e}")
        return None


def main():
    api_token = get_netdisco_session()
    if not api_token:
        print("🛑 Синхронизация отменена из-за ошибки авторизации в Netdisco.")
        return

    req_headers = {"Authorization": api_token}
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    
    # Запрашиваем роль Switch строго по slug
    switch_role = nb.dcim.device_roles.get(slug="switch")
    if not switch_role:
        print("🛑 Критическая ошибка: Роль со slug 'switch' отсутствует в NetBox. Пересоздание невозможно.")
        return

    # Запрашиваем ВСЕ активные устройства из NetBox
    nb_devices = list(nb.dcim.devices.filter(status="active"))
    print(f"\n🔄 Запуск безопасного пересоздания коммутаторов. Всего устройств на проверку: {len(nb_devices)}")

    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()
        current_role_slug = nb_dev.role.slug if nb_dev.role else ""

        # СТРОЖАЙШИЙ ФИЛЬТР БЕЗОПАСНОСТИ: Обрабатываем ТОЛЬКО устройства с ролью Switch
        if current_role_slug != "switch":
            continue

        # Фильтр по наличию локального конфига устройства
        file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
        if not os.path.exists(file_path):
            print(f"⏭️  {device_name}: роль 'switch', но локальный конфиг отсутствует. Пропускаем.")
            continue

        # Резолвим IP через DNS
        management_ip = resolve_dns(device_name)
        if not management_ip:
            print(f"❌ {device_name}: Не резолвится в DNS. Пропускаем.")
            continue

                # Проверяем текущую панель интерфейсов на самом устройстве
        current_interfaces = list(nb.dcim.interfaces.filter(device_id=nb_dev.id))
        
        current_type_name = nb_dev.device_type.model if nb_dev.device_type else ""
        current_vendor = nb_dev.device_type.manufacturer.name if (nb_dev.device_type and nb_dev.device_type.manufacturer) else ""
        current_role_slug = nb_dev.role.slug if nb_dev.role else ""

        # Узнаем, сколько портов ДОЛЖНО БЫТЬ по заводскому шаблону
        templated_ports_count = 0
        if nb_dev.device_type:
            # Запрашиваем шаблоны интерфейсов, привязанные к этой модели
            interface_templates = list(nb.dcim.interface_templates.filter(device_type_id=nb_dev.device_type.id))
            templated_ports_count = len(interface_templates)

        # СТРОГОЕ УСЛОВИЕ ПЕРЕСОЗДАНИЯ:
        # 1. Модель или Вендор содержат "Generic"
        # 2. Количество портов на устройстве НЕ СОВПАДАЕТ с количеством портов в его шаблоне
        need_recreate = (
            "generic" in current_type_name.lower() or
            "generic" in current_vendor.lower() or
            len(current_interfaces) != templated_ports_count
        )

        if not need_recreate:
            print(f"🔹 {device_name}: Шаблон '{current_type_name}' и все порты ({len(current_interfaces)} из {templated_ports_count}) корректны. Пропускаем.")
            continue

        print(f"\n🚨 Коммутатор {device_name} подлежит пересозданию!")
        print(f"   [Детали]: Портов в NetBox: {len(current_interfaces)} | Ожидается по шаблону: {templated_ports_count} | Модель: '{current_type_name}'")


        # Получаем правильную модель из Netdisco по точному IP
        disco_model_name = None
        try:
            res = requests.get(f"{NETDISCO_URL}/api/v1/object/device/{management_ip}", headers=req_headers)
            if res.status_code == 200:
                csv_text = res.content.decode('utf-8')
                reader = csv.DictReader(io.StringIO(csv_text.strip()))
                search_data = list(reader)
                if search_data:
                    disco_device = search_data[0]
                    disco_model_name = disco_device.get("Model", "").strip()
        except Exception as e:
            print(f"   ⚠️ Ошибка запроса к Netdisco для {device_name}: {e}")

        # Подстраховка по имени модели
        if not disco_model_name:
            if current_type_name and "generic" not in current_type_name.lower():
                disco_model_name = current_type_name
            else:
                print(f"   ❌ Не удалось определить заводскую модель через Netdisco. Пропускаем.")
                continue

        # Проверяем, загружен ли такой Device Type с GitHub в NetBox
        target_device_type = nb.dcim.device_types.get(model=disco_model_name)
        if not target_device_type:
            print(f"   ❌ В NetBox отсутствует фабричный шаблон для модели '{disco_model_name}'.")
            print(f"      👉 Сначала запустите import_device_types.py для этой модели.")
            continue

        # Сохраняем метаданные коммутатора для бережного переноса
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

        # БЕЗОПАСНОЕ ПЕРЕСОЗДАНИЕ КОММУТАТОРА
        print(f"   🧹 Удаление старого некорректного коммутатора '{device_name}'...")
        try:
            # Снимаем блокировки NetBox, если на старом объекте висел Primary IP
            if nb_dev.primary_ip4:
                ip_id = nb_dev.primary_ip4.id
                nb_dev.primary_ip4 = None
                nb_dev.save()
                nb.ipam.ip_addresses.delete([ip_id])

            nb_dev.delete()
            
            # Создаем чистый объект, NetBox автоматически применит шаблон портов с GitHub
            new_dev = nb.dcim.devices.create(**device_payload)
            print(f"   🎉 Коммутатор пересоздан с шаблоном '{disco_model_name}'. Порты сгенерированы.")
            
        except Exception as e:
            print(f"   ❌ Ошибка при пересоздании коммутатора: {e}")
            continue

        # НАПОЛНЕНИЕ СЕТЕВЫХ ДАННЫХ ИЗ КОНФИГА
        target_iface_name = get_interface_from_config(device_name, management_ip)
        if not target_iface_name:
            print(f"   ⚠️  IP {management_ip} не найден в конфиге. Подстраховка: 'mgmt0'")
            target_iface_name = "mgmt0"

        # Ищем порт в развернутой NetBox-ом фабричной панели интерфейсов
        target_interface = get_or_create_interface(nb, new_dev, target_iface_name)
        
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
                    print(f"   ✅ Сетевой адрес {management_ip} успешно привязан к интерфейсу '{target_iface_name}'.")
            except Exception as e:
                print(f"   ❌ Не удалось связать IP-адрес с обновленным интерфейсом: {e}")
                print("\n🎉 Безопасное пересоздание коммутаторов завершено!")
if __name__ == "__main__":
    main()