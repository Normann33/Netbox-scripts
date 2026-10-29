import os
import keyring
import requests
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

# --- НАСТРОЙКИ NETDISCO ---
NETDISCO_URL = os.getenv("NETDISCO_URL")
NETDISCO_USER = os.getenv("NETDISCO_USER")
NETDISCO_PASS = keyring.get_password("Netdisco", NETDISCO_USER)

# --- НАСТРОЙКИ ПАПОК (Переиспользуем из первого скрипта) ---
RAW_CONFIGS_DIR = "~/backbone/configs"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ""  

RAW_LIBRARY_DIR = "~/device-types/Cisco"
LIBRARY_DIR = os.path.expanduser(RAW_LIBRARY_DIR)
DNS_SUFFIX = os.getenv("DNS_SUFFIX", "").strip()


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
    """
    Пытается разрешить имя устройства в IP-адрес.
    Если задан DNS_SUFFIX, автоматически подставляет его.
    """
    hostname = device_name
    
    # Если суффикс задан и имя устройства еще не содержит его
    if DNS_SUFFIX and not hostname.endswith(DNS_SUFFIX):
        # Убираем лишние точки по краям и собираем FQDN
        suffix_clean = DNS_SUFFIX.lstrip('.')
        hostname = f"{device_name}.{suffix_clean}"
        
    try:
        # Попытка 1: Ищем по полному доменному имени (FQDN)
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        # Попытка 2 (Подстраховка): Если FQDN не нашел, пробуем по короткому имени
        if hostname != device_name:
            try:
                return socket.gethostbyname(device_name)
            except socket.gaierror:
                pass
        return None


def normalize_port_name(port_name):
    """
    Приводит сокращения и грязные строки портов к чистому фабричному виду NetBox.
    Пример: 'Gi1/0/23 vrf man' -> 'GigabitEthernet1/0/23'
    """
    if not port_name:
        return ""
    
    p_clean = port_name.strip()
    
    # 1. Извлекаем строго префикс и индекс (цифры и слэши), отсекая VRF и прочий мусор
    # Ищет что-то вроде Gi1/0/23 или Eth1/45
    match = re.search(r"^([A-Za-z-]+(?:\s*[A-Za-z-]+)*)\s*(\d+(?:/\d+)*)", p_clean)
    if not match:
        return p_clean
        
    prefix = match.group(1).strip().lower()
    index = match.group(2).strip()
    
    # 2. Маппинг префиксов
    if prefix in ["gigabitethernet", "gig", "gi", "g"]:
        return f"GigabitEthernet{index}"
    elif prefix in ["tengigabitethernet", "ten", "te", "t"]:
        return f"TenGigabitEthernet{index}"
    elif prefix in ["fastethernet", "fast", "fa", "f"]:
        return f"FastEthernet{index}"
    elif prefix in ["ethernet", "eth", "et", "e"]:
        return f"Ethernet{index}"
    elif prefix in ["port-channel", "po"]:
        return f"Port-channel{index}"
        
    return f"{prefix}{index}"


def parse_infostring(infostring, current_device_name):
    """Построчно парсит HTML-строку INFOSTRING, извлекая чистые линки"""
    links = []
    if not infostring:
        return links

    short_name = current_device_name.split('.')[0].lower()
    lines = re.split(r"<br\s*/?>", infostring, flags=re.IGNORECASE)
    
    # Список стоп-слов, которые регулярка ошибочно может принять за имя устройства
    stop_words = ["no", "to", "vlan", "none", "connected", "not"]

    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # <b>хост:порт</b> (сосед удаленный_порт)
        match = re.search(r"<b>([^:]+):([^<]+)</b>\s*\(([^ ]+)\s+([^)]+)\)", line, re.IGNORECASE)
        if match:
            host_part = match.group(1).strip().split('.')[0].lower()
            local_port = match.group(2).strip()
            remote_dev = match.group(3).strip().split('.')[0]
            remote_port = match.group(4).strip()
            
            # Фильтруем мусорные совпадения
            if remote_dev.lower() in stop_words or len(remote_dev) < 3:
                continue

            if host_part == short_name:
                links.append({
                    "local_port": normalize_port_name(local_port),
                    "remote_device": remote_dev,
                    "remote_port": normalize_port_name(remote_port)
                })
                
    return links


def get_interface_from_config(device_name, management_ip):
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
    if not os.path.exists(file_path):
        return None
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


def extract_part_number_from_config(device_name):
    file_path = os.path.join(CONFIGS_DIR, f"{device_name}{CONFIG_EXTENSION}")
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
    if not os.path.exists(LIBRARY_DIR): return None, None
    for root, dirs, files in os.walk(LIBRARY_DIR):
        for file in files:
            if file.endswith((".yaml", ".yml")):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        tpl_data = yaml.safe_load(f)
                        if tpl_data and (str(tpl_data.get("part_number", "")).lower() == part_number.lower() or 
                                         str(tpl_data.get("model", "")).lower() == part_number.lower()):
                            return tpl_data, file_path
                except Exception: continue
    return None, None


def create_device_on_the_fly(nb, device_name, switch_role):
    """
    Автоматически создает устройство в NetBox с нуля, если для него обнаружен
    локальный файл конфигурации, импортируя и разворачивая фабричный шаблон портов.
    """
    print(f"   🛠️  [Авто-Создание] Обнаружен конфиг для '{device_name}'. Запуск создания устройства...")
    
    management_ip = resolve_dns(device_name)
    if not management_ip:
        print(f"     ❌ Устройство {device_name} не резолвится в DNS. Создание отменено.")
        return None
        
    part_number = extract_part_number_from_config(device_name)
    if not part_number:
        print(f"     ❌ Не удалось извлечь PID/Партномер из конфига {device_name}.")
        return None
        
    yaml_data, yaml_path = find_local_template_by_pn(part_number)
    if not yaml_data:
        print(f"     ❌ Локальный YAML-шаблон для партномера '{part_number}' не найден в библиотеке.")
        return None
        
    vendor_name = os.path.basename(os.path.dirname(yaml_path)).title()
    model_name = yaml_data.get("model")
    
    # Гарантируем наличие Device Type в NetBox
    existing_type = nb.dcim.device_types.get(model=model_name)
    if not existing_type:
        try:
            slug_vendor = vendor_name.lower().replace(" ", "-").replace(".", "")
            manufacturer = nb.dcim.manufacturers.get(slug=slug_vendor) or nb.dcim.manufacturers.create(name=vendor_name, slug=slug_vendor)
            
            type_payload = {
                "manufacturer": manufacturer.id, "model": model_name,
                "slug": yaml_data.get("slug") or model_name.lower().replace(" ", "-").replace("/", "-"),
                "part_number": yaml_data.get("part_number", "")
            }
            existing_type = nb.dcim.device_types.create(**type_payload)
            
            if "interfaces" in yaml_data and yaml_data["interfaces"]:
                for iface in yaml_data["interfaces"]:
                    nb.dcim.interface_templates.create(device_type=existing_type.id, name=iface.get("name"), type=iface.get("type", "1000base-t"))
        except Exception as e:
            print(f"     ❌ Не удалось зарегистрировать тип устройства {model_name}: {e}")
            return None

    # Ищем дефолтный сайт (например, первый доступный в NetBox, так как мы создаем на лету)
    sites = list(nb.dcim.sites.all())
    default_site_id = sites[0].id if sites else None

    # Создаем устройство
    try:
        new_dev = nb.dcim.devices.create(
            name=device_name,
            device_type=existing_type.id,
            role=switch_role.id,
            site=default_site_id,
            status="active"
        )
        print(f"     ✅ Карточка коммутатора '{device_name}' создана. Панель портов развернута.")
        
        # Навешиваем сетевой адрес
        target_iface_name = get_interface_from_config(device_name, management_ip) or "mgmt0"
        target_interface = nb.dcim.interfaces.get(device_id=new_dev.id, name=target_iface_name)
        if not target_interface:
            iface_type = "virtual" if any(kw in target_iface_name.lower() for kw in ["vlan", "loopback", "lo"]) else "1000base-t"
            target_interface = nb.dcim.interfaces.create(device=new_dev.id, name=target_iface_name, type=iface_type)
            
        ip_object = nb.ipam.ip_addresses.get(address=management_ip) or nb.ipam.ip_addresses.create(
            address=f"{management_ip}/32", assigned_object_type="dcim.interface", assigned_object_id=target_interface.id
        )
        if ip_object:
            ip_object.assigned_object_type = "dcim.interface"
            ip_object.assigned_object_id = target_interface.id
            ip_object.save()
            new_dev.primary_ip4 = ip_object.id
            new_dev.save()
            print(f"     ✅ Менеджмент IP {management_ip} успешно привязан к порту '{target_iface_name}' и сделан Primary.")
            return nb.dcim.devices.get(id=new_dev.id)
    except Exception as e:
        print(f"     ❌ Ошибка создания объекта в API: {e}")
        return None


def main():
    api_token = get_netdisco_session()
    if not api_token: 
        return
    req_headers = {"Authorization": api_token}
    
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    switch_role = nb.dcim.device_roles.get(slug="switch")
    
    nb_switches = list(nb.dcim.devices.filter(status="active", role="switch", has_primary_ip=True))
    print(f"🔄 Всего активных коммутаторов в NetBox: {len(nb_switches)}. Запуск прокладки линков...")
    processed_links = set()
    
    for nb_dev in nb_switches:
        device_name = nb_dev.name.strip()
        management_ip = nb_dev.primary_ip4.address.split('/')[0]
        
        print(f"\n🔎 [Анализ карты LLDP для] {device_name} ({management_ip})...")
        neighbors_url = f"{NETDISCO_URL}/api/v1/object/device/{management_ip}/neighbors"
        
        try:
            res = requests.get(neighbors_url, headers=req_headers)
            if res.status_code != 200: 
                continue
                
            links_list = res.json().get("data", {}).get("links", [])
            for link_item in links_list:
                from_id = link_item.get("FROMID", "").strip()
                to_id = link_item.get("TOID", "").strip()
                infostring = link_item.get("INFOSTRING", "")
                
                if from_id == management_ip or to_id == management_ip:
                    discovered_links = parse_infostring(infostring, device_name)
                    
                    for link in discovered_links:
                        local_port_name = link["local_port"]
                        remote_device_name = link["remote_device"]
                        remote_port_name = link["remote_port"]
                        
                        # Пропускаем агрегированные каналы Port-Channel
                        if local_port_name.lower().startswith(("port-channel", "po")) or remote_port_name.lower().startswith(("port-channel", "po")):
                            continue
                            
                        # 1. Поиск соседа в NetBox (с авто-подстановкой префикса ag-)
                        nb_neighbor_dev = nb.dcim.devices.get(name=remote_device_name)
                        if not nb_neighbor_dev:
                            alt_name = f"ag-{remote_device_name}"
                            nb_neighbor_dev = nb.dcim.devices.get(name=alt_name)
                            if nb_neighbor_dev:
                                remote_device_name = alt_name
                                
                        # 2. ТРИГГЕР НА АВТО-СОЗДАНИЕ: Если соседа нет, но для него ЕСТЬ файл бэкапа
                        if not nb_neighbor_dev:
                            # Проверяем как чистое имя, так и имя с префиксом ag-
                            if os.path.exists(os.path.join(CONFIGS_DIR, remote_device_name)):
                                nb_neighbor_dev = create_device_on_the_fly(nb, remote_device_name, switch_role)
                            elif os.path.exists(os.path.join(CONFIGS_DIR, f"ag-{remote_device_name}")):
                                nb_neighbor_dev = create_device_on_the_fly(nb, f"ag-{remote_device_name}", switch_role)
                                if nb_neighbor_dev: 
                                    remote_device_name = f"ag-{remote_device_name}"
                                    
                        if not nb_neighbor_dev:
                            print(f"    skip: Сосед '{remote_device_name}' не найден в NetBox and не имеет локального конфига.")
                            continue
                            
                        # 3. Исключаем не-коммутаторы из топологии линков
                        if nb_neighbor_dev.role.slug != "switch":
                            continue
                            
                        # Защита от дублирования кабелей
                        pair_key = tuple(sorted([nb_dev.id, nb_neighbor_dev.id]))
                        link_hash = f"{pair_key}{local_port_name}{remote_port_name}"
                        if link_hash in processed_links: 
                            continue
                        processed_links.add(link_hash)
                        
                        # 4. Поиск портов на устройствах
                        local_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=local_port_name)
                        remote_iface = nb.dcim.interfaces.get(device_id=nb_neighbor_dev.id, name=remote_port_name)
                        
                        # Кросс-преобразование имен (если 3750 называет порт Gi1/0/23, а на Nexus он Eth1/45)
                        # Если порт не найден, пробуем поискать его по индексу (цифрам), подстраиваясь под вендора
                        if not local_iface and "ethernet" in local_port_name.lower():
                            idx = re.search(r"(\d+(?:/\d+)*)$", local_port_name).group(1)
                            local_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=f"GigabitEthernet{idx}")
                            
                        if not remote_iface and "ethernet" in remote_port_name.lower():
                            idx = re.search(r"(\d+(?:/\d+)*)$", remote_port_name).group(1)
                            remote_iface = nb.dcim.interfaces.get(device_id=nb_neighbor_dev.id, name=f"GigabitEthernet{idx}")
                            
                        if not local_iface:
                            print(f"    ❌ На локальном '{nb_dev.name}' не найден фабричный порт '{local_port_name}'")
                            continue
                        if not remote_iface:
                            print(f"    ❌ На удаленном '{remote_device_name}' не найден фабричный порт '{remote_port_name}'")
                            continue
                            
                        # Проверяем занятость портов кабелями
                        if local_iface.cable or remote_iface.cable:
                            is_already_connected = False
                            if local_iface.link_peers:
                                peer_ids = [p.id for p in local_iface.link_peers] if isinstance(local_iface.link_peers, list) else [local_iface.link_peers.id]
                                if remote_iface.id in peer_ids: 
                                    is_already_connected = True
                                    
                            if is_already_connected:
                                print(f"    🔹 {nb_dev.name} [{local_iface.name}] <---> [{remote_iface.name}] {nb_neighbor_dev.name} уже соединены. Пропуск.")
                                continue
                            else:
                                print(f"    ⚠️  Конфликт: порт {local_iface.name} на {nb_dev.name} или порт соседа занят.")
                                continue
                                
                        # Прокладываем физический кабель
                        print(f"    🔗 Прокладка кабеля: {nb_dev.name} [{local_iface.name}] <---> [{remote_iface.name}] {nb_neighbor_dev.name}...")
                        try:
                            nb.dcim.cables.create(
                                a_terminations=[{"object_type": "dcim.interface", "object_id": local_iface.id}],
                                b_terminations=[{"object_type": "dcim.interface", "object_id": remote_iface.id}],
                                status="connected"
                            )
                            print("       ✅ Успешно создан!")
                        except Exception as e:
                            print(f"       ❌ Ошибка API NetBox: {e}")
                            
        except Exception as e:
            print(f"  ⚠️  Ошибка при обработке LLDP для {nb_dev.name}: {e}")
            
    print("\n🎉 Построение сетевой топологии с авто-наполнением успешно завершено!")


if __name__ == "__main__":
    main()
