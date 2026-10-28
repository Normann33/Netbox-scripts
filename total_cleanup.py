import os
import keyring
import pynetbox
from dotenv import load_dotenv

load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

nb = pynetbox.api(netbox_url, token=netbox_token)

def total_cleanup():
    print("🧹 Запуск тотальной очистки NetBox по интерфейсам...")
    
    devices = list(nb.dcim.devices.filter(status="active"))
    print(f"📋 Найдено активных устройств: {len(devices)}")
    
    for dev in devices:
        print(f"\n🔎 Проверка устройства: {dev.name}")
        
        # 1. Сбрасываем Primary IP, если он вдруг где-то заполнен
        if dev.primary_ip4:
            print(f"  🔄 Сброс заполненного Primary IP")
            try:
                dev.primary_ip4 = None
                dev.save()
            except Exception as e:
                print(f"    ❌ Ошибка сброса поля Primary IP: {e}")

        # 2. Ищем интерфейсы управления и чистим их содержимое
        interfaces = list(nb.dcim.interfaces.filter(device_id=dev.id))
        for iface in interfaces:
            if iface.name in ["Loopback0", "mgmt0"]:
                # Находим ВСЕ IP-адреса, привязанные к этому конкретному интерфейсу
                assigned_ips = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
                
                if assigned_ips:
                    print(f"  🗑️ Найдено IP-адресов на интерфейсе '{iface.name}': {len(assigned_ips)}. Удаляем...")
                    for ip in assigned_ips:
                        try:
                            ip.delete()
                            print(f"    ✅ IP {ip.address} полностью удален из IPAM.")
                        except Exception as e:
                            print(f"    ❌ Не удалось удалить IP {ip.address}: {e}")
                
                # После удаления IP удаляем сам интерфейс
                print(f"  🗑️ Удаление пустого интерфейса '{iface.name}'...")
                try:
                    iface.delete()
                    print(f"    ✅ Интерфейс '{iface.name}' успешно удален.")
                except Exception as e:
                    print(f"    ❌ Не удалось удалить интерфейс '{iface.name}': {e}")

    print("\n🎉 Тотальная очистка по интерфейсам завершена! База идеально чиста.")

if __name__ == "__main__":
    total_cleanup()
