from typing import Dict, List, Optional
from collections import deque
from sqlalchemy.orm import Session
import models

# ponytail: in-memory alert log & console dispatcher, upgrade to Zalo ZNS / Twilio SMS / FCM SDK khi cấu hình API key nhà mạng
# M4: deque maxlen=1000 để giới hạn bộ nhớ (không tăng vô hạn theo thời gian)
DISPATCHED_NOTIFICATIONS: deque = deque(maxlen=1000)

def dispatch_family_alert(
    elderly_id: str,
    title: str,
    message: str,
    alert_type: str,
    db: Session,
    location: Optional[str] = None
) -> Dict:
    """
    Gửi cảnh báo khẩn cấp tới số điện thoại người nhà / người chăm sóc chính.
    alert_type: 'SOS_EMERGENCY', 'PUBLIC_QR_SCAN', 'MEDICATION_MISSED'
    """
    members = (
        db.query(models.FamilyMember)
        .filter(models.FamilyMember.elderly_id == elderly_id)
        .order_by(models.FamilyMember.is_primary_contact.desc())
        .all()
    )

    now_str = models.get_vn_now().strftime("%H:%M %d/%m/%Y")
    sent_count = 0
    recipients = []

    for m in members:
        payload = {
            "family_member_id": m.id,
            "recipient_name": m.full_name,
            "phone_number": m.phone_number,
            "elderly_id": elderly_id,
            "alert_type": alert_type,
            "title": title,
            "message": message,
            "location": location or "Tại nhà",
            "sent_at": now_str,
            "status": "SENT"
        }
        DISPATCHED_NOTIFICATIONS.append(payload)
        recipients.append({"name": m.full_name, "phone": m.phone_number})
        sent_count += 1
        try:
            print(f"[FAMILY_ALERT_DISPATCH] Gui {alert_type} toi {m.full_name} ({m.phone_number}): {title}")
        except Exception:
            pass

    return {
        "success": True,
        "dispatched_count": sent_count,
        "recipients": recipients,
        "sent_at": now_str
    }
