import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
from database import SessionLocal
import models
from main import app

client = TestClient(app)

def test_family_history():
    # 1. Test endpoint GET /api/family/lan_1952/history
    res = client.get("/api/family/lan_1952/history?days=7")
    assert res.status_code == 200, res.text
    history = res.json()
    assert len(history) == 7
    print(f"1. History items: {len(history)} days returned")
    
    today_item = history[-1]
    assert "date" in today_item
    assert "total_doses" in today_item
    assert "taken_doses" in today_item
    assert "compliance_rate" in today_item
    assert today_item["total_doses"] == 2  # 2 meds with 1 dose each
    print(f"2. Today total expected doses: {today_item['total_doses']}")

    # 2. Test days bound clamp (e.g. days=50 -> clamped to 30)
    res_clamped = client.get("/api/family/lan_1952/history?days=50")
    assert res_clamped.status_code == 200
    assert len(res_clamped.json()) == 30
    print("3. Clamped 30 days max verified")

    # 3. Test summary endpoint total_today calculation
    res_sum = client.get("/api/family/summary/lan_1952")
    assert res_sum.status_code == 200
    sum_data = res_sum.json()
    assert sum_data["medication_stats"]["total_today"] == 2
    print("4. Summary medication_stats total_today is 2 doses")
    print("ALL_FAMILY_HISTORY_TESTS_PASSED_100%")

if __name__ == "__main__":
    test_family_history()
