import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
from database import SessionLocal
import models
from main import app

client = TestClient(app)

def test_medication_flow():
    db = SessionLocal()
    # Clean up today's logs before test
    db.query(models.MedicationLog).filter(models.MedicationLog.elderly_id == "lan_1952").delete()
    db.commit()

    try:
        # 1. Get today medications
        res = client.get("/api/elderly/lan_1952/medications/today")
        assert res.status_code == 200, res.text
        meds = res.json()
        assert len(meds) >= 2
        med1 = meds[0]
        assert med1["is_taken"] == False

        # 2. Take medication
        res_take = client.post(
            f"/api/elderly/lan_1952/medications/{med1['medication_id']}/take",
            json={"confirmation_method": "BUTTON"}
        )
        assert res_take.status_code == 200, res_take.text
        take_data = res_take.json()
        assert take_data["success"] == True
        log_id = take_data["log_id"]

        # 3. Check again today medications -> should be is_taken == True
        res_check = client.get("/api/elderly/lan_1952/medications/today")
        meds_check = res_check.json()
        m1_check = next(m for m in meds_check if m["medication_id"] == med1["medication_id"])
        assert m1_check["is_taken"] == True

        # 4. Check family summary -> compliance and taken_today updated
        res_fam = client.get("/api/family/summary/lan_1952")
        assert res_fam.status_code == 200
        fam_data = res_fam.json()
        assert fam_data["medication_stats"]["taken_today"] >= 1

        # 5. Undo take medication (scoped by elderly_id)
        res_undo = client.post(f"/api/elderly/lan_1952/medications/log/{log_id}/undo")
        assert res_undo.status_code == 200

        # 6. Verify undone
        res_final = client.get("/api/elderly/lan_1952/medications/today")
        m1_final = next(m for m in res_final.json() if m["medication_id"] == med1["medication_id"])
        assert m1_final["is_taken"] == False
        print("ALL_MEDICATION_FLOW_ASSERTIONS_PASSED_100%")

    finally:
        db.close()

if __name__ == "__main__":
    test_medication_flow()
