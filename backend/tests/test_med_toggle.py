import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
from main import app
from database import SessionLocal
import models

client = TestClient(app)

def test_medication_toggle_active():
    # 1. Fetch caregiver medications
    res = client.get("/api/family/lan_1952/medications")
    assert res.status_code == 200
    meds = res.json()
    assert len(meds) >= 2
    med1 = meds[0]
    initial_active = med1["is_active"]
    med_id = med1["id"]

    # 2. Toggle active (e.g. True -> False)
    res_toggle = client.post(f"/api/family/medications/{med_id}/toggle-active")
    assert res_toggle.status_code == 200
    data_toggle = res_toggle.json()
    assert data_toggle["success"] == True
    assert data_toggle["is_active"] != initial_active
    print("1. Toggle active success, new status:", data_toggle["is_active"])

    # 3. Verify in today's medications: if is_active is False, it should NOT appear in today's medications
    if not data_toggle["is_active"]:
        res_today = client.get("/api/elderly/lan_1952/medications/today")
        today_meds = res_today.json()
        assert not any(m["medication_id"] == med_id for m in today_meds)
        print("2. Inactive medication properly excluded from elderly today view!")

    # 4. Toggle back to restore initial state
    res_restore = client.post(f"/api/family/medications/{med_id}/toggle-active")
    assert res_restore.status_code == 200
    assert res_restore.json()["is_active"] == initial_active
    print("3. Restored initial medication active state!")
    print("ALL_MED_TOGGLE_TESTS_PASSED_100%")

if __name__ == "__main__":
    test_medication_toggle_active()
