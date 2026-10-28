import os
import keyring
import requests
import pynetbox
import csv
import io
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


def get_real_interface_name(management_ip, req_headers):
    """
    Запрашивает таблицу IP-адресов устройства из Netdisco по URL /object/device/{ip}/device_ips
    и находит точное имя интерфейса, на котором висит конкретный Management IP
    """
    device_ips_url = f"{NETDISCO_URL}/api/v1/object/device/{management_ip}/device_ips"
    
    try:
        res = requests.get(device_ips_url, headers=req_headers)
        
        if res.status_code == 200:
            ips_data = res.json()
            
            # Строго перебираем все IP-адреса устройства
            for item in ips_data:
                current_ip = item.get("ip", "").strip()
                
                # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Вытаскиваем интерфейс только для нашего конкретного менеджмент-IP!
                if current_ip == management_ip:
                    interface_name = item.get("port")
                    if interface_name:
                        return interface_name.strip()
            
            print(f"   ⚠️  Специфичный IP {management_ip} не найден в таблице /device_ips устройства.")
        else:
            print(f"   ⚠️  Не удалось получить /device_ips. Статус: {res.status_code}")
            
    except Exception as e:
        print(f"   ⚠️  Ошибка при запросе /object/device/.../device_ips для {management_ip}: {e}")
    
    return None  # Подстраховка: main() подставит 'mgmt0', если совпадение не найдено





def get_or_create_interface(nb, nb_dev, interface_name):
    """Ищет интерфейс по имени в NetBox, если не находит — создает его с правильным типом"""
    existing_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=interface_name)
    if existing_iface:
        return existing_iface
        
    print(f"   ⚠️  Интерфейс '{interface_name}' не найден в NetBox. Создаем...")
    try:
        name_lower = interface_name.lower()
        
        # 1. Проверяем на виртуальные/логические интерфейсы
        if any(kw in name_lower for kw in ["vlan", "loopback", "bvi", "lo", "port-channel", "po"]):
            iface_type = "virtual"
            
        # 2. Динамически определяем скорость для физических интерфейсов
        elif "ten" in name_lower or "tg" in name_lower:
            iface_type = "10base-x-sfpp"       # 10 Гбит/с SFP+
        elif "gig" in name_lower or "ge" in name_lower:
            iface_type = "1000base-t"          # 1 Гбит/с Copper
        elif "fast" in name_lower or "fa" in name_lower:
            iface_type = "100base-tx"          # 100 Мбит/с FastEthernet
        else:
            # Универсальный дефолт для Linux-подобных имен (eth0, enp3s0) или OOB-портов управления
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
    
    # Запрашиваем только активные устройства
    nb_devices = nb.dcim.devices.filter(status="active")
    
    print("\n🔄 Начинаем сверку устройств по имени...")
    
    for nb_dev in nb_devices:
        if not nb_dev.name:
            continue
            
        device_name = nb_dev.name.strip()

        # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Если Primary IP уже есть — полностью пропускаем устройство
        if nb_dev.primary_ip4:
            print(f"🔹 {device_name}: уже имеет Primary IP ({nb_dev.primary_ip4.address}). Пропускаем.")
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
            
        # === СТРОГАЯ ВАЛИДАЦИЯ ИМЕН ===
        disco_device = None
        for dev in search_data:
            sys_name = dev.get("System Name", "").strip()
            
            # Вариант 1: Полное совпадение (switch-3 == switch-3)
            # Вариант 2: Совпадение короткого имени с FQDN (switch-3 == switch-3.domain.local)
            if sys_name.lower() == device_name.lower() or sys_name.lower().startswith(device_name.lower() + "."):
                disco_device = dev
                break
        
        # Если после перебора точное совпадение не найдено (например, искали switch-3, а вернулся только switch-3-3)
        if not disco_device:
            print(f"❓ Точное совпадение для устройства '{device_name}' не найдено в выдаче Netdisco. Пропускаем.")
            continue
        # === КОНЕЦ ВАЛИДАЦИИ ===
        
        real_serial = disco_device.get("Serials", "").strip()
        real_os_version = disco_device.get("OS Version", "").strip()
        management_ip = disco_device.get("Management IP", "").strip()
        
        has_changes = False
        
        # Сверка серийника
        if real_serial and nb_dev.serial != real_serial:
            print(f"🔎 Найдено расхождение серийного номера для {nb_dev.name}: '{nb_dev.serial}' -> '{real_serial}'")
            nb_dev.serial = real_serial
            has_changes = True
            
        # Сверка версии ОС
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

        # 4. НАПОЛНЕНИЕ IP И ИНТЕРФЕЙСОВ (Только для пустых устройств)
        if management_ip:
            print(f"🚀 Заполнение сетевых данных для {nb_dev.name} -> IP: {management_ip}")
            
            # Узнаем реальное имя интерфейса из Netdisco
            target_iface_name = get_real_interface_name(management_ip, req_headers)
            print(f"   ℹ️ Netdisco сообщил реальный интерфейс: '{target_iface_name}'")

            # Проверяем, существует ли уже такой IP в NetBox IPAM
            ip_object = nb.ipam.ip_addresses.get(address=management_ip)

            if ip_object:
                # IP найден. Так как устройство гарантированно не имело primary_ip, 
                # просто привязываем существующий IP к нашему новому/найденному интерфейсу
                target_interface = get_or_create_interface(nb, nb_dev, target_iface_name)
                if target_interface:
                    ip_object.assigned_object_type = "dcim.interface"
                    ip_object.assigned_object_id = target_interface.id
                    ip_object.save()
            else:
                # IP не существует в NetBox, создаем его с нуля на нужном интерфейсе
                target_interface = get_or_create_interface(nb, nb_dev, target_iface_name)
                if target_interface:
                    try:
                        ip_object = nb.ipam.ip_addresses.create(
                            address=f"{management_ip}/32",
                            assigned_object_type="dcim.interface",
                            assigned_object_id=target_interface.id
                        )
                    except Exception as e:
                        print(f"   ❌ Не удалось создать IP-адрес {management_ip}: {e}")
                        ip_object = None

            # Назначаем созданный/найденный IP как Primary IPv4
            if ip_object:
                try:
                    nb_dev.primary_ip4 = ip_object.id
                    nb_dev.save()
                    print(f"   ✅ IP {management_ip} успешно назначен как Primary IPv4 на интерфейс '{target_iface_name}'.")
                except Exception as e:
                    print(f"   ❌ Ошибка при назначении Primary IP для {nb_dev.name}: {e}")
        else:
            print(f"⚠️ Устройство {device_name} найдено в Netdisco, но у него пустой Management IP.")

    print("\n🎉 Синхронизация базовых данных и IP-адресов успешно завершена!")

if __name__ == "__main__":
    main()
