import os
import re
import keyring
import pynetbox
import ipaddress
from portreplace import port_replace
from dotenv import load_dotenv

load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

# --- НАСТРОЙКИ NETBOX ---
NETBOX_URL = netbox_url
NETBOX_TOKEN = netbox_token

# --- НАСТРОЙКИ ВНУТРЕННИХ ПАПОК ---
RAW_CONFIGS_DIR = "~/config-backup/backups/lab-2801-1/"  
CONFIGS_DIR = os.path.expanduser(RAW_CONFIGS_DIR)  
CONFIG_EXTENSION = ""  # Конфиги без расширения

# Настройки подключения к NetBox
DEVICE_NAME = "mc-i-1"
CONFIG_FILE = f"{CONFIGS_DIR}mc-i-1.cfg"



nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


def clear_existing_acl_rules(acl_id):
    """Находит и удаляет все существующие правила для конкретного ACL"""
    try:
        # Фильтруем правила по ID родительского списка доступа
        old_rules = nb.plugins.access_lists.endpoint("extended-acl-rules").filter(access_list_id=acl_id)
        if old_rules:
            print(f"      [Очистка] Удаляем старые правила ({len(old_rules)} шт.) для этого ACL...")
            for rule in old_rules:
                rule.delete()
    except Exception as e:
        print(f"      [!] Не удалось очистить старые правила для ACL ID {acl_id}: {e}")


# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ВЕРХНИЙ УРОВЕНЬ)

def parse_wildcard(ip_str, wildcard_str):
    """Анализирует IP и Wildcard маску. Возвращает (список_CIDR, флаг_разрывная)."""
    if wildcard_str == "0.0.0.0":
        return [f"{ip_str}/32"], False
        
    try:
        wildcard_int = int(ipaddress.IPv4Address(wildcard_str))
        netmask_int = 4294967295 ^ wildcard_int
        netmask_str = str(ipaddress.IPv4Address(netmask_int))
        
        network = ipaddress.IPv4Network(f"{ip_str}/{netmask_str}", strict=False)
        return [str(network)], False
        
    except ValueError:
        wildcard_bytes = [int(b) for b in wildcard_str.split('.')]
        ip_bytes = [int(b) for b in ip_str.split('.')]
        
        def generate_ips(current_bytes, byte_idx):
            if byte_idx == 4:
                yield '.'.join(map(str, current_bytes))
                return
            w_byte = wildcard_bytes[byte_idx]
            i_byte = ip_bytes[byte_idx]
            base_byte = i_byte & ~w_byte
            
            possible_values = []
            for v in range(256):
                if (v & ~w_byte) == 0:
                    possible_values.append(base_byte | v)
            for val in possible_values:
                yield from generate_ips(current_bytes + [val], byte_idx + 1)

        all_ips = [ipaddress.IPv4Address(ip) for ip in generate_ips([], 0)]
        collapsed_networks = [str(net) for net in ipaddress.collapse_addresses(all_ips)]
        return collapsed_networks, True


def get_or_create_prefix_id(prefix_cidr):
    """Ищет префикс в IPAM NetBox. Если его нет — создает, возвращая его ID."""
    if not prefix_cidr:
        return None
    
    existing = nb.ipam.prefixes.get(prefix=prefix_cidr)
    if existing:
        return existing.id
    
    try:
        print(f"      [IPAM] Префикс {prefix_cidr} не найден. Создаем...")
        new_prefix = nb.ipam.prefixes.create(
            prefix=prefix_cidr,
            status="active",
            description="Auto-created by ACL Import Script"
        )
        return new_prefix.id
    except Exception as e:
        print(f"      [!] Не удалось создать префикс {prefix_cidr} в IPAM: {e}")
        return None


def get_or_create_object_group_id(group_name):
    """Ищет или создает Object-Group в плагине NetBox, возвращая её ID."""
    try:
        # Пытаемся найти существующую группу по имени
        existing = nb.plugins.access_lists.endpoint("object-groups").get(name=group_name)
        if existing:
            return existing.id
            
        # Если группы нет — создаем новую
        print(f"      [Plugin] Object-Group '{group_name}' не найдена. Создаем...")
        new_group = nb.plugins.access_lists.endpoint("object-groups").create(
            name=group_name,
            type="ip"  # Тип группы адресов в Cisco
        )
        return new_group.id
    except Exception as e:
        print(f"      [!] Ошибка работы с Object-Group '{group_name}': {e}")
        return None


def parse_cisco_extended_rule(line):
    """Разбирает строку Cisco Extended ACL (any, host, подсети, object-group)."""
    tokens = line.strip().split()
    if len(tokens) < 3:
        return None

    action = tokens[0]
    protocol = tokens[1]
    
    idx = 2

    def parse_address_block(tokens, current_idx):
        if current_idx >= len(tokens):
            return None, current_idx

        token = tokens[current_idx]

        if token == "any":
            return {"type": "any", "value": None, "wildcard": None}, current_idx + 1

        if token == "host":
            ip = tokens[current_idx + 1]
            return {"type": "host", "value": ip, "wildcard": "0.0.0.0"}, current_idx + 2

        if token == "object-group":
            group_name = tokens[current_idx + 1]
            return {"type": "object-group", "value": group_name, "wildcard": None}, current_idx + 2

        if re.match(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', token):
            ip = token
            wildcard = tokens[current_idx + 1]
            return {"type": "subnet", "value": ip, "wildcard": wildcard}, current_idx + 2

        return None, current_idx

    # Парсим Source
    source_data, idx = parse_address_block(tokens, idx)

    # Пропуск портов источника (eq/range)
    if idx < len(tokens) and tokens[idx] in ["eq", "range", "gt", "lt"]:
        if tokens[idx] == "range":
            idx += 3
        else:
            idx += 2

    # Парсим Destination
    destination_data, idx = parse_address_block(tokens, idx)

    # Порты назначения
    # Инициализируем структуры портов назначения
        # --- [После парсинга Destination адреса] ---
    # Инициализируем структуры портов назначения
    dst_port_ranges = []
    dst_port_terms = []

    if idx < len(tokens) and tokens[idx] in ["eq", "range", "gt", "lt"]:
        port_type = tokens[idx]
        
        if port_type == "eq":
            idx += 1
            # Собираем ВСЕ последующие токены портов, пока они есть
            while idx < len(tokens):
                # Очищаем токен от запятых, если они есть (например, "80," -> "80")
                raw_port = tokens[idx].replace(",", "").strip()
                if not raw_port:
                    idx += 1
                    continue
                
                # Если вдруг наткнулись на логирование в конце строки (log / log-input), останавливаемся
                if raw_port.startswith("log"):
                    break
                    
                if raw_port.isdigit():
                    p_int = int(raw_port)
                    dst_port_ranges.append([p_int, p_int])
                    dst_port_terms.append(str(p_int))
                else:
                    # Если указано имя сервиса (www, https, ntp)
                    dst_port_ranges.append([int(port_replace(raw_port)), int(port_replace(raw_port))])
                    dst_port_terms.append(int(port_replace(raw_port)))
                    
                idx += 1  # Переходим к следующему порту в строке
                
        elif port_type == "range":
            start_port = int(tokens[idx + 1])
            end_port = int(tokens[idx + 2])
            dst_port_ranges.append([start_port, end_port])
            dst_port_terms.append(f"{start_port}-{end_port}")
            
        elif port_type == "gt":
            port_num = int(tokens[idx + 1])
            dst_port_ranges.append([port_num + 1, 65535])
            dst_port_terms.append(f">{port_num}")
            
        elif port_type == "lt":
            port_num = int(tokens[idx + 1])
            dst_port_ranges.append([0, port_num - 1])
            dst_port_terms.append(f"<{port_num}")

    return {
        "action": action,
        "protocol": protocol,
        "source": source_data,
        "destination": destination_data,
        "dst_port_ranges": dst_port_ranges,
        "dst_port_terms": dst_port_terms
    }



# 3. ГЛАВНАЯ ФУНКЦИЯ ИМПОРТА

def import_acls_with_tracking():
    nb_device = nb.dcim.devices.get(name=DEVICE_NAME)
    if not nb_device:
        print(f"[!] Устройство {DEVICE_NAME} не найдено.")
        return

    current_acl = None
    rule_sequence = 10

    print(f"[*] Анализируем файл конфигурации: {CONFIG_FILE}...")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            
            if not line or line.startswith("!"):
                continue

            if line.startswith("ip access-list extended "):
                acl_name = line.split()[-1]
                
                # Ищем, существует ли уже такой ACL глобально в NetBox
                existing_acl = nb.plugins.access_lists.endpoint("access-lists").get(name=acl_name)
                
                if existing_acl:
                    print(f"\n[-] ACL '{acl_name}' уже существует. Перезаписываем его правила...")
                    current_acl = existing_acl
                    # Вызываем очистку старых правил перед записью новых
                    clear_existing_acl_rules(current_acl.id)
                else:
                    print(f"\n[+] Создаем новый заголовок ACL: {acl_name}")
                    current_acl = nb.plugins.access_lists.endpoint("access-lists").create(
                        name=acl_name, 
                        device=nb_device.id, # Привязываем к текущему устройству при первом создании
                        type="extended", 
                        default_action="deny"
                    )
                
                rule_sequence = 10
                continue

            if current_acl:
                if line.startswith(("interface", "router", "vlan", "line", "ip route")):
                    current_acl = None
                    continue

                if line.startswith(("permit", "deny")):
                    parsed_data = parse_cisco_extended_rule(line)
                    if not parsed_data:
                        continue

                    # --- ПОДГОТОВКА SOURCE ДАННЫХ ---
                    src_data = parsed_data["source"]
                    src_payload_fields = {"type": None, "id": None}
                    src_prefixes = [None]
                    src_is_dis = False

                    if src_data:
                        if src_data["type"] in ["subnet", "host"]:
                            src_prefixes, src_is_dis = parse_wildcard(src_data["value"], src_data["wildcard"])
                            src_payload_fields["type"] = "ipam.prefix"
                        elif src_data["type"] == "object-group":
                            src_payload_fields["type"] = "netbox_acls.objectgroup"
                            # ТУТ ВЫЗЫВАЕТСЯ: получаем ID группы, создавая её при необходимости
                            src_payload_fields["id"] = get_or_create_object_group_id(src_data["value"])

                    # --- ПОДГОТОВКА DESTINATION ДАННЫХ ---
                    dst_data = parsed_data["destination"]
                    dst_payload_fields = {"type": None, "id": None}
                    dst_prefixes = [None]
                    dst_is_dis = False

                    if dst_data:
                        if dst_data["type"] in ["subnet", "host"]:
                            dst_prefixes, dst_is_dis = parse_wildcard(dst_data["value"], dst_data["wildcard"])
                            dst_payload_fields["type"] = "ipam.prefix"
                        elif dst_data["type"] == "object-group":
                            dst_payload_fields["type"] = "netbox_acls.objectgroup"
                            # ТУТ ВЫЗЫВАЕТСЯ: получаем ID группы, создавая её при необходимости
                            dst_payload_fields["id"] = get_or_create_object_group_id(dst_data["value"])

                    is_discontiguous = src_is_dis or dst_is_dis
                    marker_tag = f"discontiguous_mask_line_{line_num}" if is_discontiguous else ""

                    print(f"   -> Строка {line_num}: {line} " + (f"[{marker_tag}]" if is_discontiguous else ""))

                    # --- ГЕНЕРАЦИЯ ПРАВИЛ В NETBOX ---
                    for src_net in src_prefixes:
                        for dst_net in dst_prefixes:
                            
                            payload = {
                                "access_list": current_acl.id,
                                "sequence": rule_sequence,
                                "action": parsed_data["action"],
                                "protocol": parsed_data["protocol"].lower(),
                                "description": f"{marker_tag} | Orig: {line[:60]}" if is_discontiguous else f"Orig: {line[:80]}",
                                # Добавляем порты (если правил для портов нет, отправятся пустые списки)
                                "destination_port_ranges": parsed_data["dst_port_ranges"],
                                "destination_port_terms": parsed_data["dst_port_terms"],
                                "source_port_ranges": [], # Заполняем пустыми массивами, чтобы API не ругался
                                "source_port_terms": []
                            }

                            # Привязываем Source (Prefix или Object-Group)
                            if src_payload_fields["type"] == "ipam.prefix" and src_net:
                                payload["source_type"] = "ipam.prefix"
                                payload["source_id"] = get_or_create_prefix_id(src_net)
                            elif src_payload_fields["type"] == "netbox_acls.objectgroup" and src_payload_fields["id"]:
                                payload["source_type"] = "netbox_acls.objectgroup"
                            # Привязываем Source (Prefix или Object-Group)
                            if src_payload_fields["type"] == "ipam.prefix" and src_net:
                                payload["source_type"] = "ipam.prefix"
                                payload["source_id"] = get_or_create_prefix_id(src_net)
                            elif src_payload_fields["type"] == "netbox_acls.objectgroup" and src_payload_fields["id"]:
                                payload["source_type"] = "netbox_acls.objectgroup"
                                payload["source_id"] = src_payload_fields["id"]

                            # Привязываем Destination (Prefix или Object-Group)
                            if dst_payload_fields["type"] == "ipam.prefix" and dst_net:
                                payload["destination_type"] = "ipam.prefix"
                                payload["destination_id"] = get_or_create_prefix_id(dst_net)
                            elif dst_payload_fields["type"] == "netbox_acls.objectgroup" and dst_payload_fields["id"]:
                                payload["destination_type"] = "netbox_acls.objectgroup"
                                payload["destination_id"] = dst_payload_fields["id"]

                            # Записываем порты
                            # if parsed_data["dest_port_value"]:
                            #     payload["comments"] = f"Destination ports: {parsed_data['dest_port_value']}"

                            try:
                                nb.plugins.access_lists.endpoint("extended-acl-rules").create(payload)
                                rule_sequence += 1
                            except Exception as e:
                                print(f"      [!] Ошибка создания правила. Payload: {payload}. Ошибка: {e}")
                                pass
                                
                    # Этот блок выравнивается под циклы генерации комбинаций (внутри 'if line.startswith')
                    rule_sequence = ((rule_sequence + 9) // 10) * 10


# Блок запуска скрипта (в самом низу файла, без отступов)
if __name__ == "__main__":
    import_acls_with_tracking()
