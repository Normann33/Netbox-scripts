import os
import keyring
import pynetbox
from dotenv import load_dotenv

load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

nb = pynetbox.api(netbox_url, token=netbox_token)

def total_cleanup():
    print("🧹 Запуск тотальной очистки NetBox перед пересозданием...")
    
    devices = list(nb.dcim.devices.filter(status="active"))
    print(f"📋 Найдено активных устройств: {len(devices)}")
    
    for dev in devices:
        # 1. Сбрасываем Primary IP
        if not dev.primary_ip4:
            print(f"  🔄 Нет Primary IP для {dev.name}, продолжаем")
            continue
        if dev.primary_ip4:
            print(f"  🔄 Сброс Primary IP для {dev.name}")
            
            # Сразу удаляем сам объект IP-адреса из базы IPAM, чтобы он не мешался
            try:
                ip_id = dev.primary_ip4.id
                dev.primary_ip4 = None
                dev.save()
                
                # Удаляем IP из NetBox полностью
                nb.ipam.ip_addresses.delete([ip_id])
            except Exception as e:
                print(f"    ❌ Ошибка при удалении IP для {dev.name}: {e}")
        
        # 2. Удаляем старые интерфейсы, которые мы генерировали
        interfaces = list(nb.dcim.interfaces.filter(device_id=dev.id))
        for iface in interfaces:
            if iface.name in ["Loopback0", "mgmt0"]:
                # Если на интерфейсе всё еще висят какие-то "забытые" IP — удаляем их
                assigned_ips = nb.ipam.ip_addresses.filter(interface_id=iface.id)
                for ip in assigned_ips:
                    ip.delete()
                    
                print(f"  🗑️ Удаление интерфейса '{iface.name}' на {dev.name}")
                try:
                    iface.delete()
                except Exception as e:
                    print(f"    ❌ Не удалось удалить интерфейс: {e}")

    print("\n🎉 База полностью зачищена от старых менеджмент IP и интерфейсов!")

if __name__ == "__main__":
    total_cleanup()
