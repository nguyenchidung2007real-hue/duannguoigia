import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
import models
from main import app
from database import SessionLocal
from services.notification_service import DISPATCHED_NOTIFICATIONS

client = TestClient(app)

def test_family_notification_dispatch():
    DISPATCHED_NOTIFICATIONS.clear()

    # Xóa SOS logs cũ để tránh dedup block QR alert
    db = SessionLocal()
    db.query(models.HealthAndSOSLog).filter(
        models.HealthAndSOSLog.elderly_id == "lan_1952"
    ).delete()
    db.commit()
    db.close()

    # 1. Test SOS button notification dispatch
    res_sos = client.post("/api/elderly/lan_1952/sos")
    assert res_sos.status_code == 200
    assert len(DISPATCHED_NOTIFICATIONS) >= 1
    last_notif = DISPATCHED_NOTIFICATIONS[-1]
    assert last_notif["alert_type"] == "SOS_EMERGENCY"
    assert last_notif["elderly_id"] == "lan_1952"
    assert "0912345678" in last_notif["phone_number"]
    print("1. SOS alert dispatched to caregiver:", last_notif["recipient_name"], last_notif["phone_number"])

    # 2. Test QR scan emergency alert dispatch with GPS
    res_qr = client.post(
        "/qr/qr_lan_1952/alert",
        json={"latitude": 21.01234, "longitude": 105.85432}
    )
    assert res_qr.status_code == 200
    assert len(DISPATCHED_NOTIFICATIONS) >= 2
    qr_notif = DISPATCHED_NOTIFICATIONS[-1]
    assert qr_notif["alert_type"] == "PUBLIC_QR_SCAN"
    assert "21.01234" in qr_notif["location"]
    print("2. QR rescue alert dispatched with GPS:", qr_notif["location"])

    # 3. Test GET notifications endpoint
    res_list = client.get("/api/family/lan_1952/notifications")
    assert res_list.status_code == 200
    notifs = res_list.json()
    assert len(notifs) >= 2
    assert notifs[0]["alert_type"] == "PUBLIC_QR_SCAN"  # Most recent first
    print("3. GET notifications list verified, total items:", len(notifs))
    print("ALL_NOTIFICATION_TESTS_PASSED_100%")

if __name__ == "__main__":
    test_family_notification_dispatch()
