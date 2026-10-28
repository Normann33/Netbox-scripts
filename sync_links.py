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


def get_or_create_interface(nb, nb_dev, interface_name):
    """Ищет или создает интерфейс для магистрального линка (LLDP)"""
    existing_iface = nb.dcim.interfaces.get(device_id=nb_dev.id, name=interface_name)
    if existing_iface:
        return existing_iface
        
    print(f"   ⚠️  Интерфейс '{interface_name}' не найден на {nb_dev.name}. Создаем...")
    try:
        # Для LLDP связей по умолчанию ставим тип 1000base-t (GigaEthernet)
        new_iface = nb.dcim.interfaces.create(
            device=nb_dev.id,
            name=interface_name,
            type="1000base-t"
        )
        return new_iface
    except Exception as e:
        print(f"   ❌ Не удалось создать интерфейс '{interface_name}' на {nb_dev.name}: {e}")
        return None


def sync_lldp_links(nb, nb_dev, management_ip, req_headers, processed_pairs):
    """Запрашивает связи устройства из Netdisco и строит их в NetBox"""
    links_url = f"{NETDISCO_URL}/api/v1/object/device/{management_ip}/links"
    
    try:
        res = requests.get(links_url, headers=req_headers)
        if res.status_code != 200:
            print(f"  ⚠️ Не удалось получить линки для {nb_dev.name}. Статус: {res.status_code}")
            return

        csv_text = res.content.decode('utf-8')
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        
        for row in reader:
            local_port_name = row.get("port", "").strip()
            neighbor_ip = row.get("neighbor", "").strip()
            remote_port_name = row.get("remote_port", "").strip()
            
            if not local_port_name or not neighbor_ip or not remote_port_name:
                continue
                
            # 1. Ищем устройство-соседа в NetBox по его Primary IP
            nb_neighbors = list(nb.dcim.devices.filter(ip_address=neighbor_ip))
            if not nb_neighbors:
                continue
                
            nb_neighbor_dev = nb_neighbors[0]

            # 2. Защита от дублирования проходов (чтобы не строить линк А->Б и следом Б->А)
            pair_key = tuple(sorted([nb_dev.id, nb_neighbor_dev.id]))
            link_key = f"{pair_key}_{local_port_name}_{remote_port_name}"
            if link_key in processed_pairs:
                continue
            processed_pairs.add(link_key)

            # 3. Находим или создаем интерфейсы на обоих коммутаторах
            local_iface = get_or_create_interface(nb, nb_dev, local_port_name)
            remote_iface = get_or_create_interface(nb, nb_neighbor_dev, remote_port_name)
            
            if not local_iface or not remote_iface:
                continue

            # 4. Проверяем статус соединений портов
            # В NetBox 4.x признак наличия физического кабеля — это заполненное поле .cable
            if local_iface.cable or remote_iface.cable:
                # Если порты соединены именно друг с другом — всё хорошо, пропускаем
                if local_iface.link_peers and local_iface.link_peers[0].id == remote_iface.id:
                    continue
                else:
                    # Если порты заняты чем-то другим, не ломаем чужую топологию
                    print(f"  ⚠️ Конфликт: порт {local_port_name} на {nb_dev.name} или порт соседа уже занят другим кабелем.")
                    continue

            # 5. Прокладываем кабель (Link) по правилам NetBox 4.x
            print(f"🔗 Создаем линк: {nb_dev.name} [{local_port_name}] <---> [{remote_port_name}] {nb_neighbor_dev.name}")
            try:
                nb.dcim.cables.create(
                    a_terminations=[{"object_type": "dcim.interface", "object_id": local_iface.id}],
                    b_terminations=[{"object_type": "dcim.interface", "object_id": remote_iface.id}],
                    status="connected"
                )
                print(f"   ✅ Кабель успешно создан.")
            except Exception as e:
                print(f"   ❌ Ошибка создания кабеля в NetBox: {e}")

    except Exception as e:
        print(f"  ⚠️ Ошибка при обработке связей устройства {nb_dev.name}: {e}")


def main():
    # 1. Авторизация в Netdisco
    api_token = get_netdisco_session()
    if not api_token:
        print("🛑 Синхронизация линков прервана из-за ошибки авторизации.")
        return

    req_headers = {"Authorization": api_token}
    print("✅ Авторизация в Netdisco успешна.")

    # 2. Инициализация NetBox
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    
    # Загружаем только активные устройства, у которых уже есть Primary IP
    # (так как без IP мы не сможем запросить данные из Netdisco и сопоставить соседей)
    nb_devices = list(nb.dcim.devices.filter(status="active", has_primary_ip=True))
    
    print(f"🔄 Найдено {len(nb_devices)} активных устройств с назначенными IP. Начинаем построение топологии...")
    
    # Множество для исключения повторной обработки одних и тех же стыков портов
    processed_pairs = set()

    for nb_dev in nb_devices:
        # Извлекаем "чистый" IP-адрес без маски (/32, /24 и т.д.)
        management_ip = nb_dev.primary_ip4.address.split('/')[0]
        
        if management_ip:
            sync_lldp_links(nb, nb_dev, management_ip, req_headers, processed_pairs)

    print("\n🎉 Синхронизация LLDP-линков успешно завершена!")

if __name__ == "__main__":
    main()
