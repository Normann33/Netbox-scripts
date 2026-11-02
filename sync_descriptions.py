import os
import re
import pynetbox
import keyring
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL = os.getenv("NETBOX_URL")
NETBOX_TOKEN = keyring.get_password("Netbox", "netbox")
CONFIGS_DIR = os.path.expanduser('~/backbone/configs/')

nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)

def parse_cisco_descriptions(file_path):
    descriptions = {}
    current_intf = None
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line.startswith('interface '):
                current_intf = line.replace('interface ', '').strip()
                continue
            if line == '!' and current_intf:
                current_intf = None
                continue
            if current_intf and line.startswith('description '):
                desc = line.replace('description ', '').strip()
                descriptions[current_intf] = desc
    return descriptions

if not os.path.exists(CONFIGS_DIR):
    print("Директория с конфигами не найдена")
    exit()

files = [f for f in os.listdir(CONFIGS_DIR) if os.path.isfile(os.path.join(CONFIGS_DIR, f))]
print(f"Найдено файлов: {len(files)}")

for filename in files:
    file_path = os.path.join(CONFIGS_DIR, filename)
    print(f"Обработка устройства: {filename}...")
    
    device_candidates = nb.dcim.devices.filter(name=filename)
    if not device_candidates:
        print(f"Устройство {filename} не найдено в NetBox. Пропускаем.")
        continue
        
    device = list(device_candidates)[0]
    parsed_descs = parse_cisco_descriptions(file_path)
    
    for intf_name, new_desc in parsed_descs.items():
        interface_candidates = nb.dcim.interfaces.filter(device_id=device.id, name=intf_name)
        if not interface_candidates:
            continue
            
        nb_interface = list(interface_candidates)[0]
        if nb_interface.description != new_desc:
            nb_interface.update({"description": new_desc})
            print(f"Обновлен дескрипшен на {intf_name}: {new_desc}")
