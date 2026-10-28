import os
import keyring
import pynetbox
from dotenv import load_dotenv

load_dotenv()

netbox_url = os.getenv("NETBOX_URL")
netbox_token = keyring.get_password("Netbox", "netbox")

# Инициализируем NetBox
nb = pynetbox.api(netbox_url, token=netbox_token)

def cleanup_netbox():
    print("🚀 Запуск скрипта очистки Primary IP и пустых интерфейсов в NetBox...")
    
    # 1. Получаем все активные устройства
    devices = list(nb.dcim.devices.filter(status="active"))
    print(f"📋 Найдено активных устройств для проверки: {len(devices)}")
    
    for dev in devices:
        has_changes = False
        
        # 2. Сбрасываем статус Primary IPv4
        if dev.primary_ip4:
            print(f"🔄 Сбрасываем Primary IP для устройства: {dev.name}")
            dev.primary_ip4 = None
            try:
                dev.save()
                has_changes = True
            except Exception as e:
                print(f"   ❌ Не удалось сбросить Primary IP для {dev.name}: {e}")
                continue
        
        # Перезапрашиваем актуальное состояние интерфейсов устройства
        interfaces = list(nb.dcim.interfaces.filter(device_id=dev.id))
        
        for iface in interfaces:
            # 3. Нас интересуют только автоматически созданные нами интерфейсы
            if iface.name in ["Loopback0", "mgmt0"]:
                # Проверяем, есть ли на этом интерфейсе какие-либо IP-адреса
                ip_count = len(list(nb.ipam.ip_addresses.filter(interface_id=iface.id)))
                
                # Если интерфейс остался пустой — удаляем его
                if ip_count == 0:
                    print(f"   🧹 Интерфейс '{iface.name}' на {dev.name} пуст. Удаляем...")
                    try:
                        iface.delete()
                        print(f"   🗑️  Успешно удален.")
                    except Exception as e:
                        print(f"   ⚠️  Не удалось удалить интерфейс {iface.name}: {e}")

    print("\n🎉 Очистка завершена! Теперь база готова к повторному корректному наполнению.")

if __name__ == "__main__":
    cleanup_netbox()
