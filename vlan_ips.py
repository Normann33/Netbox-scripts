import os
import re
import pynetbox
import keyring
from dotenv import load_dotenv

load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

# --- НАСТРОЙКИ NETBOX ---
NETBOX_URL = netbox_url
NETBOX_TOKEN = netbox_token

# Путь к конфигам
CONFIGS_DIR = os.path.expanduser('~/backbone/configs/')

# Инициализация API
nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)

def parse_vlan_ranges(vlan_str):
    """Разбирает строки VLAN вида '10,20-22,30' в список чисел. Игнорирует full-trunk диапазоны."""
    # Если на транке разрешено всё, игнорируем, чтобы не спамить базу 4000 виланами
    if vlan_str.strip().lower() in ['all', '1-4094', '2-4094']:
        return []
        
    vlans = []
    for part in vlan_str.split(','):
        part = part.strip()
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
                # Защита: если внутри строки кто-то прописал огромный диапазон (например, больше 100 виланов разом)
                if (end - start) > 100:
                    print(f"  [i] Пропущен подозрительно большой диапазон VLAN в транке: {part}")
                    continue
                vlans.extend(range(start, end + 1))
            except ValueError:
                continue
        else:
            if part.isdigit():
                vlans.append(int(part))
    return vlans

def cidr_from_mask(mask):
    """Конвертирует маску 255.255.255.0 в число бит (CIDR)"""
    try:
        return sum(bin(int(x)).count('1') for x in mask.split('.'))
    except Exception:
        return 24

def parse_cisco_config(file_path):
    """
    Быстрый построчный парсер конфигурации, адаптированный под вывод !VLAN:.
    Возвращает кортеж: (данные_интерфейсов, имена_vlan)
    """
    interfaces = {}
    vlan_names = {}
    current_intf = None

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            
                        # --- 1. Сбор имен VLAN с защитой от перезаписи техническим типом enet ---
            if line.startswith('!VLAN:'):
                vlan_match = re.match(r'^!VLAN:\s+(\d+)\s+(\S+)', line)
                if vlan_match:
                    vid = int(vlan_match.group(1))
                    vname = vlan_match.group(2)
                    
                    # Фильтруем мусорные заголовки и дефолтный VLAN 1
                    if vname.lower() not in ['name', '----', 'default', 'type'] and vid != 1:
                        # Если имя для этого VLAN уже найдено (например, remote-man), 
                        # игнорируем строчку со словом 'enet', чтобы не испортить данные
                        if vname.lower() == 'enet':
                            continue
                        
                        # Сохраняем имя (первое найденное будет самым правильным)
                        if vid not in vlan_names:
                            vlan_names[vid] = vname
                continue


            # --- 2. Сбор данных интерфейсов (остается прежним) ---
            if line.startswith('interface '):
                current_intf = line.replace('interface ', '').strip()
                interfaces[current_intf] = {'ip': None, 'access': None, 'trunk': []}
                continue
                
            if line == '!' and current_intf:
                current_intf = None
                continue
                
            if current_intf:
                # IP адрес
                ip_match = re.match(r'^ip\s+address\s+(\S+)\s+(\S+)', line)
                if ip_match:
                    ip, mask = ip_match.group(1), ip_match.group(2)
                    interfaces[current_intf]['ip'] = f"{ip}/{cidr_from_mask(mask)}"
                    continue

                # Access VLAN
                access_match = re.match(r'^switchport\s+access\s+vlan\s+(\d+)', line)
                if access_match:
                    interfaces[current_intf]['access'] = int(access_match.group(1))
                    continue

                # Trunk VLANs
                trunk_match = re.match(r'^switchport\s+trunk\s+allowed\s+vlan\s+(?:add\s+)?(\S+)', line)
                if trunk_match:
                    vlans = parse_vlan_ranges(trunk_match.group(1))
                    interfaces[current_intf]['trunk'].extend(vlans)
                    continue

    clean_interfaces = {k: v for k, v in interfaces.items() if v['ip'] or v['access'] or v['trunk']}
    return clean_interfaces, vlan_names


def get_or_create_vlan(vid, discovered_names):
    """
    Ищет VLAN в базе. Если не находит — создает с оригинальным именем из конфига 
    или генерирует имя по умолчанию, если имени в конфиге не нашлось.
    """
    vlan_candidates = nb.ipam.vlans.filter(vid=vid)
    if vlan_candidates:
        # vlan_candidates - это список. Берем первый элемент [0]
        return list(vlan_candidates)[0].id
    
    # Определяем имя: берем из конфига или делаем заглушку
    vlan_name = discovered_names.get(vid)
    if not vlan_name:
        vlan_name = f"VLAN{vid}_auto"

    try:
        new_vlan = nb.ipam.vlans.create(
            vid=vid,
            name=vlan_name,
            status="active"
        )
        print(f"  [+] Автоматически создан VLAN {vid} с именем: {vlan_name}")
        return new_vlan.id
    except Exception as e:
        print(f"  [X] Не удалось создать VLAN {vid}: {e}")
        return None

def parse_and_update(file_path, device_name):
    print(f"\n[i] Обработка устройства: {device_name}...")
    
    device_candidates = nb.dcim.devices.filter(name=device_name)
    if not device_candidates:
        print(f"  [!] Устройство {device_name} не найдено в NetBox. Пропускаем.")
        return
    device = list(device_candidates)[0]

    # Получаем структуру интерфейсов и словарь {vid: name} из конфига
    config_data, discovered_vlan_names = parse_cisco_config(file_path)
    
    for intf_name, data in config_data.items():
        interface_candidates = nb.dcim.interfaces.filter(device_id=device.id, name=intf_name)
        if not interface_candidates:
            print(f"  [⚠️] Интерфейс {intf_name} отсутствует в NetBox. Пропускаем.")
            continue
        nb_interface = list(interface_candidates)[0]

        interface_payload = {}

        # 1. ОБРАБОТКА TRUNK
        if data['trunk']:
            nb_vlan_ids = []
            for vid in set(data['trunk']):
                # Автосоздаем VLAN в транке, ТОЛЬКО если для него нашлось реальное имя в таблице !VLAN
                # В противном случае ищем только среди уже существующих в NetBox (из RackTables)
                if vid in discovered_vlan_names:
                    vlan_id = get_or_create_vlan(vid, discovered_vlan_names)
                else:
                    # Ищем существующий, если нет - пропускаем (не плодим авто-виланы из транков)
                    vlan_candidates = nb.ipam.vlans.filter(vid=vid)
                    vlan_id = list(vlan_candidates)[0].id if vlan_candidates else None
                
                if vlan_id:
                    nb_vlan_ids.append(vlan_id)
            
            if nb_vlan_ids:
                interface_payload['mode'] = 'tagged'
                interface_payload['tagged_vlans'] = nb_vlan_ids

        # 2. ОБРАБОТКА ACCESS
        elif data['access']:
            # На access-портах виланы создавать можно всегда (они единичные)
            vlan_id = get_or_create_vlan(data['access'], discovered_vlan_names)
            if vlan_id:
                interface_payload['mode'] = 'access'
                interface_payload['untagged_vlan'] = vlan_id

        # Обновляем L2 параметры интерфейса
        if interface_payload:
            nb_interface.update(interface_payload)
            print(f"  [~] Интерфейс {intf_name} обновлен (VLANs)")

        # 3. ОБРАБОТКА IP (Защита от разницы масок /24 и /32)
        if data['ip']:
            # Выделяем чистый IP без маски (например, '172.16.x.11')
            pure_ip = data['ip'].split('/')[0]
            
            # Ищем в NetBox адрес по чистому IP, игнорируя маску
            ip_candidates = nb.ipam.ip_addresses.filter(q=pure_ip)
            
            ip_payload = {
                'address': data['ip'], # Сюда передаем правильный адрес с маской из конфига (e.g. /24)
                'status': 'active',
                'assigned_object_type': 'dcim.interface',
                'assigned_object_id': nb_interface.id
            }
            
            # Проверяем, есть ли точное совпадение по чистому IP
            exact_candidate = None
            if ip_candidates:
                for candidate in ip_candidates:
                    if candidate.address.split('/')[0] == pure_ip:
                        exact_candidate = candidate
                        break
            
            if not exact_candidate:
                # Если такого IP вообще нет в базе — создаем с нуля
                try:
                    nb.ipam.ip_addresses.create(**ip_payload)
                    print(f"    [+] Добавлен новый IP {data['ip']} на {intf_name}")
                except Exception as e:
                    print(f"    [X] Ошибка создания IP {data['ip']}: {e}")
            else:
                # IP найден (например, старый /32). Исправляем его маску и привязываем к порту
                if exact_candidate.assigned_object_id != nb_interface.id or exact_candidate.address != data['ip']:
                    try:
                        exact_candidate.update(ip_payload)
                        print(f"    [~] IP {pure_ip} обновлен (маска/привязка) на интерфейсе {intf_name}")
                    except Exception as e:
                        print(f"    [X] Не удалось обновить IP {data['ip']}: {e}")
                else:
                    print(f"    [i] IP {data['ip']} уже корректно настроен на {intf_name}")


def main():
    if not os.path.exists(CONFIGS_DIR):
        print(f"[X] Директория {CONFIGS_DIR} не существует!")
        return

    files = [f for f in os.listdir(CONFIGS_DIR) if os.path.isfile(os.path.join(CONFIGS_DIR, f))]
    print(f"[i] Найдено {len(files)} файлов конфигурации.")

    for filename in files:
        file_path = os.path.join(CONFIGS_DIR, filename)
        try:
            parse_and_update(file_path, filename)
        except Exception as e:
            print(f"[X] Ошибка при обработке устройства {filename}: {e}")

if __name__ == "__main__":
    main()